// The cafe's Mini App: menu → item options → cart → checkout. Plain JS, no build step.
// The menu comes embedded in the page (web/miniapp.py); customer data and orders go through the API, signed with
// Telegram's initData. The cart is kept on the phone (localStorage, per cafe); the server re-checks it.
(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  const D = JSON.parse(document.getElementById("data").textContent);
  const initData = tg ? tg.initData : "";
  const items = new Map();
  D.menu.forEach((c) => c.items.forEach((it) => items.set(it.id, it)));
  const user = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;

  const state = {
    lang: user && user.language_code === "uz" ? "uz" : "ru",
    view: "menu", // menu | cart | done
    cart: loadCart(),
    // street: the line under the pin (fills itself from the map until the guest edits it), details: entrance, flat…
    form: { method: "pickup", phone: "", street: "", details: "", comment: "", payment: D.payments[0], point: null },
    streetAuto: true,
    sending: false,
    done: null,
  };
  let sheet = null; // the open item: { it, chosen: Set of option ids, qty }

  const $ = (sel) => document.querySelector(sel);
  const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => ENTITIES[ch]);
  const T = (key, vars = {}) => (D.texts[state.lang][key] || key).replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
  const currency = () => (state.lang === "uz" && D.currency === "сум" ? "soʻm" : D.currency);
  const money = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " " + currency();

  // --- cart ---
  function storage() {
    try {
      return window.localStorage;
    } catch {
      return null; // blocked storage: the cart just is not kept between visits
    }
  }
  function loadCart() {
    try {
      return JSON.parse(storage()?.getItem("cart:" + D.slug) || "[]").filter(valid);
    } catch {
      return [];
    }
  }
  function saveCart() {
    try {
      storage()?.setItem("cart:" + D.slug, JSON.stringify(state.cart));
    } catch {
      // see storage()
    }
  }
  // the same rule as Store.pick: exactly one option in every single-choice group, only known options
  function pick(it, ids) {
    const chosen = new Set(ids);
    const picked = [];
    for (const g of it.groups) {
      const inGroup = g.options.filter((o) => chosen.has(o.id));
      if (!g.multiple && g.options.length && inGroup.length !== 1) return null;
      picked.push(...inGroup);
    }
    return picked.length === chosen.size ? picked : null;
  }
  function valid(line) {
    const it = items.get(line.item_id);
    return !!it && Number.isInteger(line.qty) && line.qty > 0 && Array.isArray(line.option_ids) && !!pick(it, line.option_ids);
  }
  const unit = (l) => items.get(l.item_id).price + pick(items.get(l.item_id), l.option_ids).reduce((s, o) => s + o.price, 0);
  const subtotal = () => state.cart.reduce((s, l) => s + unit(l) * l.qty, 0);
  const count = () => state.cart.reduce((s, l) => s + l.qty, 0);
  function lineTitle(l) {
    const it = items.get(l.item_id);
    const options = pick(it, l.option_ids).map((o) => o.name);
    return options.length ? `${it.name} (${options.join(", ")})` : it.name;
  }
  function add(item_id, option_ids, qty) {
    const line = { item_id, option_ids: [...option_ids].sort((a, b) => a - b), qty };
    const same = state.cart.find((l) => l.item_id === item_id && l.option_ids.join(".") === line.option_ids.join("."));
    if (same) same.qty = Math.min(99, same.qty + qty);
    else state.cart.push(line);
    saveCart();
  }
  function change(i, delta) {
    state.cart[i].qty = Math.min(99, state.cart[i].qty + delta);
    if (state.cart[i].qty <= 0) state.cart.splice(i, 1);
    saveCart();
  }

  // --- delivery: price by the pin on the map (the same rule as delivery.fee on the server) ---
  function km(a, b) {
    const rad = (x) => (x * Math.PI) / 180;
    const h = Math.sin(rad(b[0] - a[0]) / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(rad(b[1] - a[1]) / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(h));
  }
  function quote() {
    const p = state.form.point;
    const zones = D.delivery.zones;
    const dist = p && D.delivery.point ? km(D.delivery.point, p) : null;
    if (!zones.length) return { ok: true, fee: D.delivery.fee, km: dist };
    if (!p) return { ok: false, need: "set_point" };
    const zone = zones.find(([limit]) => dist <= limit);
    return zone ? { ok: true, fee: zone[1], km: dist } : { ok: false, need: "far", km: dist };
  }
  function quoteText(q) {
    if (!q.ok) return T(q.need, { km: D.delivery.zones.length ? D.delivery.zones[D.delivery.zones.length - 1][0] : "" });
    if (q.km === null) return T("map_hint");
    return `${T("distance", { km: q.km.toFixed(1).replace(".", ",") })} · ${T("delivery_fee")}: ${money(q.fee)}`;
  }

  // The map is made once and moved into each new render of the cart, so re-renders do not reload it.
  // MapLibre 4.7.1 (static/vendor, BSD) + OpenFreeMap tiles: free, no keys, commercial use allowed.
  const MAP_JS = "/static/vendor/maplibre-gl.js";
  const MAP_CSS = "/static/vendor/maplibre-gl.css";
  const MAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
  let map = null;
  let mapBox = null;
  let mapFailed = false;
  function mapElement() {
    if (mapBox) return mapBox;
    mapBox = document.createElement("div");
    mapBox.className = "mapbox";
    mapBox.innerHTML = `<div class="canvas"></div>
      <svg class="pin" viewBox="0 0 24 36" aria-hidden="true"><path d="M12 0C5.4 0 0 5.3 0 11.9 0 20.8 12 36 12 36s12-15.2 12-24.1C24 5.3 18.6 0 12 0z"/><circle cx="12" cy="12" r="4.5"/></svg>
      <button type="button" class="locate" data-locate>${T("locate")}</button>`;
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = MAP_CSS;
    document.head.append(css);
    const js = document.createElement("script");
    js.src = MAP_JS;
    js.onload = startMap;
    js.onerror = mapError;
    document.head.append(js);
    if (tg && tg.isVersionAtLeast && tg.isVersionAtLeast("7.7")) tg.disableVerticalSwipes(); // a swipe on the map must not close the app
    return mapBox;
  }
  function startMap() {
    const at = state.form.point || D.delivery.center;
    try {
      map = new maplibregl.Map({
        container: mapBox.querySelector(".canvas"),
        style: MAP_STYLE,
        center: [at[1], at[0]],
        zoom: state.form.point ? 16 : D.delivery.point ? 13 : 11,
        attributionControl: { compact: true },
        dragRotate: false,
        pitchWithRotate: false,
      });
    } catch {
      return mapError(); // no WebGL
    }
    map.touchZoomRotate.disableRotation();
    map.on("moveend", (e) => {
      if (!e.originalEvent && !e.byUs) return; // only where the guest moved it
      const c = map.getCenter();
      setPoint([c.lat, c.lng]);
    });
    const failed = setTimeout(mapError, 20000);
    map.once("load", () => clearTimeout(failed));
  }
  function mapError() {
    mapFailed = true;
    if (state.view === "cart") render();
  }
  function setPoint(point) {
    state.form.point = point;
    updateQuote();
    suggestStreet(point);
  }
  // the street and house under the pin (server: geocode.py) — only into a line the guest has not typed themselves
  let lookup = 0;
  let lookupTimer;
  function suggestStreet(point) {
    clearTimeout(lookupTimer);
    if (!initData || !state.streetAuto) return;
    const input = $("[name=street]");
    if (input) input.placeholder = T("street_looking");
    const mine = ++lookup;
    lookupTimer = setTimeout(() => {
      api("GET", `/where?lat=${point[0].toFixed(6)}&lon=${point[1].toFixed(6)}&lang=${state.lang}`)
        .then((res) => {
          if (mine !== lookup || !state.streetAuto) return; // the pin moved again, or the guest started typing
          state.form.street = res.address || "";
          const field = $("[name=street]");
          if (field) field.value = state.form.street;
        })
        .catch(() => {})
        .finally(() => {
          const field = $("[name=street]");
          if (field && mine === lookup) field.placeholder = T("street_hint");
        });
    }, 500);
  }
  let locating = false;
  function locate() {
    if (locating) return;
    const buttons = [...document.querySelectorAll("[data-locate]")];
    const busy = (on) => {
      locating = on;
      buttons.forEach((b) => {
        b.textContent = T(on ? "locating" : "locate");
        b.disabled = on;
      });
    };
    const done = (message) => {
      busy(false);
      if (message) toast(message);
    };
    const go = (lat, lon) => {
      done();
      if (map) map.jumpTo({ center: [lon, lat], zoom: 17 }, { byUs: true });
      else setPoint([lat, lon]);
      haptic("light");
    };
    busy(true);
    setTimeout(() => locating && done(T("locate_failed")), 15000); // no answer at all: never leave it stuck
    const browser = () =>
      navigator.geolocation
        ? navigator.geolocation.getCurrentPosition(
            (pos) => go(pos.coords.latitude, pos.coords.longitude),
            (err) => done(T(err && err.code === 1 ? "locate_denied" : "locate_failed")),
            { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 },
          )
        : done(T("locate_failed"));
    const lm = tg && tg.isVersionAtLeast && tg.isVersionAtLeast("8.0") && tg.LocationManager;
    if (!lm) return browser();
    const ask = () =>
      lm.getLocation((loc) => {
        if (loc) return go(loc.latitude, loc.longitude);
        // refused now or earlier: Telegram can open its settings, but only on a tap — so say where to look
        done(T(lm.isAccessRequested && !lm.isAccessGranted ? "locate_denied" : "locate_failed"));
      });
    if (lm.isInited) return lm.isLocationAvailable ? ask() : browser();
    lm.init(() => (lm.isLocationAvailable ? ask() : browser()));
  }

  // --- views ---
  function render() {
    $("#lang").textContent = state.lang === "uz" ? "RU" : "UZ";
    $("#status").textContent = !D.active ? T("inactive") : D.open ? D.hours : T("closed", { hours: D.hours });
    if (state.view === "cart" && !state.cart.length) state.view = "menu";
    ({ menu: renderMenu, cart: renderCart, done: renderDone })[state.view]();
    if (sheet) renderSheet();
    backButton(state.view === "cart" || !!sheet);
  }

  function renderMenu() {
    const inCart = (id) => state.cart.filter((l) => l.item_id === id).reduce((s, l) => s + l.qty, 0);
    const card = (it) => {
      const n = inCart(it.id);
      const photo = it.thumb
        ? `<img src="${esc(it.thumb)}" alt="" loading="lazy">`
        : `<span class="noph">${esc(it.name.slice(0, 1))}</span>`;
      return `<button class="item" type="button" data-item="${it.id}">
        <span class="ph">${photo}${n ? `<span class="qty">${n}</span>` : ""}</span>
        <span class="name">${esc(it.name)}</span><span class="price">${money(it.price)}</span></button>`;
    };
    const chips = D.menu.map((c) => `<a href="#cat-${c.id}" class="chip">${esc(c.name)}</a>`).join("");
    const sections = D.menu
      .map((c) => `<section id="cat-${c.id}" class="cat"><h2>${esc(c.name)}</h2>
        <div class="grid">${c.items.map(card).join("")}</div></section>`)
      .join("");
    const bar = count()
      ? `<div class="bar"><button type="button" class="primary split" data-go="cart">
          <span>${T("cart")} · ${count()}</span><span>${money(subtotal())}</span></button></div>`
      : "";
    $("#view").innerHTML = (D.menu.length ? `<nav class="chips">${chips}</nav>${sections}` : `<p class="empty">${T("empty_menu")}</p>`) + bar;
  }

  function renderSheet() {
    const { it, chosen, qty } = sheet;
    const options = pick(it, [...chosen]);
    const price = (it.price + (options || []).reduce((s, o) => s + o.price, 0)) * qty;
    const groups = it.groups
      .map((g, gi) => `<div class="group"><h3>${esc(g.name)} <small>${T(g.multiple ? "pick_any" : "pick_one")}</small></h3>
        <div class="opts">${g.options
          .map((o) => `<button type="button" class="opt${chosen.has(o.id) ? " on" : ""}" data-group="${gi}" data-opt="${o.id}">
            ${esc(o.name)}${o.price ? ` <small>+${money(o.price)}</small>` : ""}</button>`)
          .join("")}</div></div>`)
      .join("");
    $("#sheet-body").innerHTML = `
      ${it.photo ? `<img class="sheet-photo" src="${esc(it.photo)}" alt="">` : ""}
      <div class="sheet-content"><h2>${esc(it.name)}</h2>
        ${it.description ? `<p class="muted">${esc(it.description)}</p>` : ""}${groups}</div>
      <div class="sheet-actions">
        <div class="stepper"><button type="button" data-sq="-1">−</button><span>${qty}</span><button type="button" data-sq="1">+</button></div>
        <button type="button" class="primary grow" data-add ${options ? "" : "disabled"}>${T("add")} · ${money(price)}</button>
      </div>`;
  }

  function renderCart() {
    const f = state.form;
    const sub = subtotal();
    const deliveryOk = D.delivery.enabled && sub >= D.delivery.min;
    if (f.method === "delivery" && !deliveryOk) f.method = "pickup";
    const q = f.method === "delivery" ? quote() : { ok: true, fee: 0 };
    const fee = q.ok ? q.fee : 0;
    const canOrder = !!initData && D.active && D.open && q.ok;
    const why = !D.active ? T("inactive") : !D.open ? T("closed", { hours: D.hours }) : T("open_in_telegram");
    const lines = state.cart
      .map((l, i) => `<li><div class="grow"><b>${esc(lineTitle(l))}</b><small>${money(unit(l))}</small></div>
        <div class="stepper small"><button type="button" data-ch="${i}:-1">−</button><span>${l.qty}</span>
        <button type="button" data-ch="${i}:1">+</button></div></li>`)
      .join("");
    const seg = (name, value, label, disabled = false) =>
      `<button type="button" data-${name}="${value}" class="${f[name === "pay" ? "payment" : "method"] === value ? "on" : ""}"
        ${disabled ? "disabled" : ""}>${label}</button>`;
    const methods = `<div class="seg">${seg("method", "pickup", T("pickup"))}
        ${D.delivery.enabled ? seg("method", "delivery", T("delivery"), !deliveryOk) : ""}</div>
      ${D.delivery.enabled && !deliveryOk ? `<p class="hint">${T("delivery_from", { min: money(D.delivery.min) })}</p>` : ""}
      ${f.method === "pickup" && D.pickup_address ? `<p class="hint">📍 ${esc(D.pickup_address)}</p>` : ""}`;
    const address = f.method === "delivery"
      ? `<div class="field"><span>${T("map_title")}</span>
          ${mapFailed ? `<p class="hint warn">${T("map_failed")}</p><button type="button" class="link" data-locate>${T("locate")}</button>` : `<div id="map-slot"></div>`}
          <p class="hint${q.ok ? "" : " warn"}" id="quote">${quoteText(q)}</p></div>
        <label>${T("street")}<input name="street" autocomplete="street-address" value="${esc(f.street)}" placeholder="${T("street_hint")}"></label>
        <label>${T("details")}<input name="details" value="${esc(f.details)}" placeholder="${T("details_hint")}"></label>`
      : "";
    const share = tg && tg.isVersionAtLeast && tg.isVersionAtLeast("6.9")
      ? `<button type="button" class="link" data-share>${T("share_phone")}</button>` : "";
    const payment = D.payments.length > 1
      ? `<div class="field"><span>${T("payment")}</span><div class="seg">${D.payments.map((p) => seg("pay", p, T(p))).join("")}</div></div>`
      : "";
    $("#view").innerHTML = `<div class="page">
        <button type="button" class="back" data-go="menu">← ${T("back")}</button>
        <div class="row"><h2>${T("your_order")}</h2><button type="button" class="link" data-clear>${T("clear")}</button></div>
        <ul class="lines">${lines}</ul>
        <h2>${T("checkout")}</h2>${methods}${address}
        <label>${T("phone")}<input name="phone" type="tel" inputmode="tel" autocomplete="tel" value="${esc(f.phone)}" placeholder="+998 90 123 45 67"></label>
        ${share}
        <label>${T("comment")}<input name="comment" maxlength="300" value="${esc(f.comment)}" placeholder="${T("comment_hint")}"></label>
        ${payment}
        ${f.payment === "cash" && D.payment_note ? `<p class="hint">💳 ${esc(D.payment_note)}</p>` : ""}
        <ul class="summary" id="summary">${summary(sub, fee)}</ul>
        ${canOrder || !q.ok ? "" : `<p class="hint warn">${why}</p>`}
      </div>
      <div class="bar"><button type="button" class="primary" data-confirm ${canOrder && !state.sending ? "" : "disabled"}>
        ${T("confirm", { total: money(sub + fee) })}</button></div>`;
    const slot = $("#map-slot");
    if (slot) slot.replaceWith(mapElement());
  }
  function summary(sub, fee) {
    return `<li><span>${T("subtotal")}</span><span>${money(sub)}</span></li>
      ${fee ? `<li><span>${T("delivery_fee")}</span><span>${money(fee)}</span></li>` : ""}
      <li class="total"><span>${T("total")}</span><span>${money(sub + fee)}</span></li>`;
  }
  // the pin moved: only the price parts change, the rest of the page (and what is being typed) stays
  function updateQuote() {
    if (state.view !== "cart" || state.form.method !== "delivery") return;
    const q = quote();
    const sub = subtotal();
    const fee = q.ok ? q.fee : 0;
    const el = $("#quote");
    el.textContent = quoteText(q);
    el.classList.toggle("warn", !q.ok);
    $("#summary").innerHTML = summary(sub, fee);
    const button = $("[data-confirm]");
    button.textContent = T("confirm", { total: money(sub + fee) });
    button.disabled = !(initData && D.active && D.open && q.ok) || state.sending;
  }

  function renderDone() {
    const d = state.done;
    const transfer = d.card
      ? `<div class="card-transfer"><p>${T("transfer_text", { total: money(d.total), card: esc(d.card.number), holder: esc(d.card.holder) })}</p>
          <button type="button" data-copy="${esc(d.card.number)}">${T("copy")}</button></div>`
      : "";
    $("#view").innerHTML = `<div class="page done"><div class="check">✓</div>
        <h2>${T("done_title", { number: d.number })}</h2><p class="muted">${T("done_text")}</p>${transfer}</div>
      <div class="bar"><button type="button" class="primary" data-closeapp>${T("close")}</button></div>`;
  }

  // --- actions ---
  function openItem(id) {
    const it = items.get(id);
    const defaults = it.groups.filter((g) => !g.multiple && g.options.length).map((g) => g.options[0].id);
    sheet = { it, chosen: new Set(defaults), qty: 1 };
    $("#sheet").hidden = false;
    document.body.classList.add("locked");
    render();
  }
  function closeSheet() {
    sheet = null;
    $("#sheet").hidden = true;
    document.body.classList.remove("locked");
    render();
  }
  function toggleOption(gi, id) {
    const g = sheet.it.groups[gi];
    if (g.multiple) sheet.chosen.has(id) ? sheet.chosen.delete(id) : sheet.chosen.add(id);
    else {
      g.options.forEach((o) => sheet.chosen.delete(o.id));
      sheet.chosen.add(id);
    }
    renderSheet();
  }
  function go(view) {
    state.view = view;
    render();
    window.scrollTo(0, 0);
  }

  async function api(method, path, body) {
    const res = await fetch(`/app/${D.slug}${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": initData },
      body: body ? JSON.stringify(body) : undefined,
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(json.error || "network"), { code: json.error || "network" });
    return json;
  }

  async function submit() {
    const f = state.form;
    // what is missing is said here, at its field, instead of after a round trip
    const missing = f.method === "delivery" && !f.street.trim() ? ["street", "error_address"]
      : (f.phone.match(/\d/g) || []).length < 7 ? ["phone", "error_phone"] : null;
    if (missing) {
      toast(T(missing[1]));
      const field = $(`[name=${missing[0]}]`);
      if (field) field.focus();
      return;
    }
    state.sending = true;
    render();
    try {
      const res = await api("POST", "/orders", {
        lines: state.cart,
        method: f.method,
        phone: f.phone,
        address: f.method === "delivery" ? [f.street.trim(), f.details.trim()].filter(Boolean).join(", ") : "",
        lat: f.method === "delivery" && f.point ? f.point[0] : null,
        lon: f.method === "delivery" && f.point ? f.point[1] : null,
        comment: f.comment,
        payment: f.payment,
        lang: state.lang,
      });
      state.done = res;
      state.cart = [];
      saveCart();
      state.view = "done";
      haptic("success");
    } catch (err) {
      toast(T("error_" + err.code, { min: money(D.delivery.min) }));
      if (err.code === "menu_changed") setTimeout(() => location.reload(), 1500); // the cart is re-checked on load
    } finally {
      state.sending = false;
      render();
    }
  }

  function sharePhone() {
    tg.requestContact((ok, res) => {
      const phone = ok && res && res.responseUnsafe && res.responseUnsafe.contact && res.responseUnsafe.contact.phone_number;
      if (!phone) return;
      state.form.phone = phone.startsWith("+") ? phone : "+" + phone;
      render();
    });
  }

  function haptic(kind) {
    try {
      if (kind === "success") tg.HapticFeedback.notificationOccurred("success");
      else tg.HapticFeedback.impactOccurred("light");
    } catch {
      // not in Telegram or an old client
    }
  }
  let toastTimer;
  function toast(text) {
    const el = $("#toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 3500);
  }
  function backButton(show) {
    if (!tg || !tg.BackButton || !tg.isVersionAtLeast("6.1")) return;
    if (show) tg.BackButton.show();
    else tg.BackButton.hide();
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("button, [data-close]");
    if (!el) return;
    const d = el.dataset;
    if ("item" in d) openItem(+d.item);
    else if ("close" in d) closeSheet();
    else if ("opt" in d) toggleOption(+d.group, +d.opt);
    else if ("sq" in d) {
      sheet.qty = Math.max(1, Math.min(99, sheet.qty + +d.sq));
      renderSheet();
    } else if ("add" in d) {
      add(sheet.it.id, [...sheet.chosen], sheet.qty);
      haptic("light");
      closeSheet();
    } else if ("go" in d) go(d.go);
    else if ("ch" in d) {
      const [i, delta] = d.ch.split(":").map(Number);
      change(i, delta);
      render();
    } else if ("clear" in d) {
      state.cart = [];
      saveCart();
      go("menu");
    } else if ("method" in d) {
      state.form.method = d.method;
      render();
    } else if ("pay" in d) {
      state.form.payment = d.pay;
      render();
    } else if ("share" in d) sharePhone();
    else if ("locate" in d) locate();
    else if ("confirm" in d) submit();
    else if ("copy" in d) navigator.clipboard?.writeText(d.copy).then(() => toast(T("copied")), () => {});
    else if ("closeapp" in d) (tg && initData ? tg.close() : go("menu"));
    else if (el.id === "lang") {
      state.lang = state.lang === "uz" ? "ru" : "uz";
      render();
      if (initData) api("POST", "/lang", { lang: state.lang }).catch(() => {});
    }
  });
  // typing must not re-render (it would drop the focus): just remember the values
  document.addEventListener("input", (e) => {
    if (e.target.name in state.form) state.form[e.target.name] = e.target.value;
    if (e.target.name === "street") state.streetAuto = !e.target.value.trim(); // typed: the map no longer overwrites it
  });

  if (tg) {
    tg.ready();
    tg.expand();
    if (document.documentElement.dataset.theme !== "auto") {
      // the cafe keeps its own look: paint Telegram's header and background to match the page
      const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      tg.setHeaderColor(bg);
      tg.setBackgroundColor(bg);
    }
    tg.BackButton.onClick(() => (sheet ? closeSheet() : go("menu")));
  }
  render();
  if (initData) {
    const params = new URLSearchParams(location.search);
    const repeat = params.get("repeat");
    if (repeat) history.replaceState(null, "", location.pathname); // a reload must not repeat it again
    api("GET", "/me" + (repeat ? `?repeat=${encodeURIComponent(repeat)}` : ""))
      .then((me) => {
        state.lang = me.lang;
        state.form.phone ||= me.phone || "";
        if (me.address && !state.form.street) {
          state.form.street = me.address; // last time's address; moving the pin elsewhere replaces it
        }
        state.form.point ||= me.point;
        if (me.point && map) map.jumpTo({ center: [me.point[1], me.point[0]], zoom: 16 });
        if (me.repeat) {
          state.cart = []; // "repeat" means exactly that order, at today's prices
          me.repeat.lines.forEach((l) => add(l.item_id, l.option_ids, l.qty));
          if (state.cart.length) state.view = "cart";
          if (me.repeat.missing.length) toast(T("repeat_missing", { names: me.repeat.missing.join(", ") }));
        }
        render();
      })
      .catch(() => {});
  }
})();
