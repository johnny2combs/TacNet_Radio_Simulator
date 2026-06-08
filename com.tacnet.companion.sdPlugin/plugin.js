// ============================================================
//  TacNet Companion Plugin – plugin.js
//  Polls radio_client.py local API (port 8895) for telemetry.
//  Actions:
//    com.tacnet.companion.status   – Radio Status Monitor
//    com.tacnet.companion.teammate – Teammate Range Grid
//    com.tacnet.companion.nav      – Objective Compass
//    com.tacnet.companion.radiocheck – Radio Check trigger
// ============================================================

var websocket = null;
var pluginUUID = null;
var activeContexts = {};  // context -> { action, settings, device }

var companionState = {
    radio: null,      // from /api/status (radio_client.py)
    clients: null,    // from /api/clients (radio_web.py relay or radio_client)
    nav: null         // from /api/nav (radio_client.py)
};

var isFetching = false;
var animFrame = 0;
var radioCheckFlash = {};  // context -> flash countdown

// ============================================================
//  STREAM DECK ENTRY POINT
// ============================================================
function connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    pluginUUID = inUUID;
    websocket = new WebSocket("ws://127.0.0.1:" + inPort);

    websocket.onopen = function () {
        websocket.send(JSON.stringify({ "event": inRegisterEvent, "uuid": inUUID }));
    };

    websocket.onmessage = function (evt) {
        var jsonObj = JSON.parse(evt.data);
        var event   = jsonObj["event"];
        var action  = jsonObj["action"];
        var context = jsonObj["context"];
        var payload = jsonObj["payload"] || {};

        if (event === "willAppear") {
            activeContexts[context] = {
                action:   action,
                settings: payload.settings || {},
                device:   jsonObj["device"],
                controller: payload.controller
            };
            updateContextState(context);
        }

        if (event === "willDisappear") {
            delete activeContexts[context];
            delete radioCheckFlash[context];
        }

        if (event === "didReceiveSettings") {
            if (activeContexts[context]) {
                activeContexts[context].settings = payload.settings || {};
                updateContextState(context);
            }
        }

        if (event === "keyDown") {
            handleKeyDown(action, context, payload);
        }
    };

    // Poll every 2 seconds
    setInterval(fetchAndRefresh, 2000);
    fetchAndRefresh();
}

function connectElgatoStreamDeck(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo);
}

// ============================================================
//  FETCH
// ============================================================
function getClientPort(context) {
    if (activeContexts[context] && activeContexts[context].settings.client_port) {
        return parseInt(activeContexts[context].settings.client_port);
    }
    return 8895;
}

function getServerPort() {
    for (var ctx in activeContexts) {
        if (activeContexts[ctx].settings && activeContexts[ctx].settings.server_port) {
            return parseInt(activeContexts[ctx].settings.server_port);
        }
    }
    return 8890;
}

function fetchAndRefresh() {
    if (isFetching) return;
    var anyActive = false;
    for (var c in activeContexts) { anyActive = true; break; }
    if (!anyActive) return;

    isFetching = true;
    var clientPort = 8895;
    for (var ctx in activeContexts) {
        if (activeContexts[ctx].settings.client_port) {
            clientPort = parseInt(activeContexts[ctx].settings.client_port);
            break;
        }
    }

    var pending = 3;
    function done() { pending--; if (pending <= 0) { isFetching = false; refreshAll(); } }

    // /api/status from radio_client.py
    fetchJSON("http://127.0.0.1:" + clientPort + "/api/status", function (data) {
        if (data) companionState.radio = data;
        done();
    });

    // /api/clients from radio_client.py (forwards server client list)
    fetchJSON("http://127.0.0.1:" + clientPort + "/api/clients", function (data) {
        if (data) companionState.clients = data;
        done();
    });

    // /api/nav from radio_client.py
    fetchJSON("http://127.0.0.1:" + clientPort + "/api/nav", function (data) {
        if (data) companionState.nav = data;
        done();
    });
}

function fetchJSON(url, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", url, true);
    xhr.timeout = 1500;
    xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) {
            if (xhr.status === 200) {
                try { callback(JSON.parse(xhr.responseText)); }
                catch (e) { callback(null); }
            } else {
                callback(null);
            }
        }
    };
    xhr.ontimeout = function () { callback(null); };
    try { xhr.send(); } catch (e) { callback(null); }
}

function postJSON(url, body, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.timeout = 2000;
    xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) {
            if (callback) {
                try { callback(JSON.parse(xhr.responseText)); }
                catch (e) { callback(null); }
            }
        }
    };
    xhr.ontimeout = function () { if (callback) callback(null); };
    try { xhr.send(JSON.stringify(body)); } catch (e) { if (callback) callback(null); }
}

// ============================================================
//  REFRESH ALL
// ============================================================
function refreshAll() {
    animFrame++;
    for (var ctx in activeContexts) {
        updateContextState(ctx);
    }
}

function updateContextState(context) {
    if (!activeContexts[context]) return;
    var action = activeContexts[context].action;
    var settings = activeContexts[context].settings;

    var canvas = document.createElement("canvas");
    canvas.width  = 144;
    canvas.height = 144;
    var ctx = canvas.getContext("2d");

    if (action === "com.tacnet.companion.status") {
        drawRadioStatus(ctx, canvas.width, canvas.height, companionState.radio);
    } else if (action === "com.tacnet.companion.teammate") {
        var callsign = settings.callsign || "ALPHA-2";
        drawTeammateGrid(ctx, canvas.width, canvas.height, callsign, companionState.clients);
    } else if (action === "com.tacnet.companion.nav") {
        drawCompass(ctx, canvas.width, canvas.height, companionState.nav);
    } else if (action === "com.tacnet.companion.radiocheck") {
        var flash = radioCheckFlash[context] || 0;
        drawRadioCheck(ctx, canvas.width, canvas.height, flash > 0);
        if (flash > 0) {
            radioCheckFlash[context] = flash - 1;
            if (flash - 1 > 0) {
                (function(c) {
                    setTimeout(function() { updateContextState(c); }, 300);
                })(context);
            }
        }
    } else {
        drawOffline(ctx, canvas.width, canvas.height, "COMPANION");
    }

    var imageData = canvas.toDataURL("image/png").replace("data:image/png;base64,", "");
    setKeyImage(context, imageData);
}

// ============================================================
//  SET KEY IMAGE
// ============================================================
function setKeyImage(context, base64png) {
    if (!websocket) return;
    websocket.send(JSON.stringify({
        "event":   "setImage",
        "context": context,
        "payload": {
            "image":   "data:image/png;base64," + base64png,
            "target":  0
        }
    }));
}

// ============================================================
//  DRAW HELPERS & HUD UTILITIES
// ============================================================
function drawHUDBackground(ctx, x, y, w, h, theme, isActive) {
    var cx = x + w / 2;
    var cy = y + h / 2;
    var grad = ctx.createRadialGradient(cx, cy, 10, cx, cy, w * 0.75);
    
    var colorCenter = "#1e293b";
    var colorEdge = "#0f172a";
    
    if (theme === "red" || theme === "warn") {
        colorCenter = isActive ? "#500a0a" : "#2d0808";
        colorEdge = isActive ? "#180202" : "#0f0202";
    } else if (theme === "green" || theme === "nominal") {
        colorCenter = isActive ? "#064e3b" : "#1e293b";
        colorEdge = isActive ? "#022c22" : "#0f172a";
    } else if (theme === "amber" || theme === "caution" || theme === "yellow") {
        colorCenter = isActive ? "#451a03" : "#1e293b";
        colorEdge = isActive ? "#1c0b02" : "#0f172a";
    } else if (theme === "orange" || theme === "emi") {
        colorCenter = isActive ? "#431407" : "#1e293b";
        colorEdge = isActive ? "#220802" : "#0f172a";
    } else if (theme === "purple" || theme === "sat") {
        colorCenter = isActive ? "#2d1060" : "#130a24";
        colorEdge = isActive ? "#1a0a3a" : "#080312";
    } else {
        // default slate
        colorCenter = isActive ? "#1e293b" : "#0f172a";
        colorEdge = isActive ? "#0f172a" : "#090d16";
    }
    
    grad.addColorStop(0, colorCenter);
    grad.addColorStop(1, colorEdge);
    ctx.fillStyle = grad;
    ctx.fillRect(x, y, w, h);
}

function drawHUDScanlines(ctx, w, h) {
    ctx.strokeStyle = "rgba(255, 255, 255, 0.03)";
    ctx.lineWidth = 1.0;
    ctx.beginPath();
    for (var y = 2; y < h; y += 4) {
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
    }
    ctx.stroke();
}

function applyNeonGlow(ctx, color, blur) {
    ctx.shadowColor = color;
    ctx.shadowBlur = blur || 10;
}

function clearNeonGlow(ctx) {
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
}

function drawShadowText(ctx, text, x, y, font, color, align) {
    ctx.font = font;
    ctx.fillStyle = color;
    ctx.textAlign = align || "center";
    ctx.shadowColor = "rgba(0, 0, 0, 0.8)";
    ctx.shadowBlur = 2;
    ctx.shadowOffsetX = 1;
    ctx.shadowOffsetY = 1;
    ctx.fillText(text, x, y);
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetX = 0;
    ctx.shadowOffsetY = 0;
}

function drawOffline(ctx, w, h, label) {
    drawHUDBackground(ctx, 0, 0, w, h, "red", true);
    ctx.strokeStyle = "#ef4444";
    ctx.lineWidth = 4;
    ctx.strokeRect(4, 4, w - 8, h - 8);
    
    ctx.fillStyle = "#ef4444";
    ctx.font = "bold 12px monospace";
    ctx.textAlign = "center";
    ctx.fillText(label, w/2, h/2 - 5);
    ctx.fillStyle = "#ffffff";
    ctx.fillText("OFFLINE", w/2, h/2 + 10);
    drawHUDScanlines(ctx, w, h);
}

function hexBar(ctx, x, y, barW, barH, value, maxVal, color) {
    // background
    ctx.fillStyle = "#1e2530";
    ctx.fillRect(x, y, barW, barH);
    // fill
    var fill = Math.max(0, Math.min(1, value / maxVal));
    ctx.fillStyle = color;
    ctx.fillRect(x, y, Math.round(barW * fill), barH);
    // border
    ctx.strokeStyle = "#334";
    ctx.lineWidth = 1;
    ctx.strokeRect(x, y, barW, barH);
}

// ============================================================
//  RADIO STATUS MONITOR
// ============================================================
function drawRadioStatus(ctx, w, h, data) {
    var isTx = !!(data && data.radio && data.radio.transmitting);
    drawHUDBackground(ctx, 0, 0, w, h, "slate", isTx);

    // Header bar
    var gradient = ctx.createLinearGradient(0, 0, w, 0);
    gradient.addColorStop(0, isTx ? "#500a0a" : "#0a2a4a");
    gradient.addColorStop(1, isTx ? "#180202" : "#0d3d6b");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = isTx ? "#ef4444" : "#38bdf8";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("RADIO STATUS", w/2, 14);

    if (!data) {
        drawOfflineOverlay(ctx, w, h);
        drawHUDScanlines(ctx, w, h);
        return;
    }

    var radio = data.radio || {};
    var pos   = data.position || {};

    // Channel & Freq
    ctx.fillStyle = "#a0aab4";
    ctx.font = "bold 8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("CH", 6, 36);
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 13px monospace";
    ctx.fillText("CH-" + (radio.channel || "--"), 6, 50);

    ctx.fillStyle = "#a0aab4";
    ctx.font = "bold 8px monospace";
    ctx.fillText("FREQ", 70, 36);
    ctx.fillStyle = "#38bdf8";
    ctx.font = "bold 9px monospace";
    ctx.fillText((radio.frequency || "---") + " MHz", 70, 50);

    // Divider
    ctx.strokeStyle = "#1e3050";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(6, 56); ctx.lineTo(w-6, 56); ctx.stroke();

    // RSSI bar
    ctx.fillStyle = "#7090a0";
    ctx.font = "8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("RSSI", 6, 68);
    var rssi = data.rssi !== undefined ? data.rssi : -120;
    var rssiNorm = Math.max(0, Math.min(1, (rssi + 120) / 80)); // -120 -> -40
    var rssiColor = rssiNorm > 0.6 ? "#22c55e" : rssiNorm > 0.3 ? "#facc15" : "#ef4444";
    hexBar(ctx, 30, 60, 78, 10, rssiNorm, 1, rssiColor);
    ctx.fillStyle = "#ccc";
    ctx.textAlign = "right";
    ctx.fillText(rssi + "dB", w-6, 68);

    // Battery bar
    ctx.fillStyle = "#7090a0";
    ctx.textAlign = "left";
    ctx.fillText("BAT", 6, 84);
    var bat = data.battery !== undefined ? data.battery : 0;
    var batColor = bat > 50 ? "#22c55e" : bat > 20 ? "#facc15" : "#ef4444";
    hexBar(ctx, 30, 76, 78, 10, bat, 100, batColor);
    ctx.fillStyle = "#ccc";
    ctx.textAlign = "right";
    ctx.fillText(bat + "%", w-6, 84);

    // Divider
    ctx.strokeStyle = "#1e3050";
    ctx.beginPath(); ctx.moveTo(6, 90); ctx.lineTo(w-6, 90); ctx.stroke();

    // Encryption status
    var enc = radio.encryption;
    ctx.fillStyle = "#7090a0";
    ctx.font = "8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("ENC", 6, 103);
    ctx.fillStyle = enc ? "#22c55e" : "#ef4444";
    ctx.font = "bold 8px monospace";
    ctx.fillText(enc ? "SECURE" : "OPEN", 34, 103);

    // TX state
    var tx = radio.transmitting;
    ctx.fillStyle = tx ? "#ef4444" : "#7090a0";
    ctx.font = tx ? "bold 8px monospace" : "8px monospace";
    ctx.textAlign = "right";
    ctx.fillText(tx ? "● TX" : "○ RX", w-6, 103);

    // MGRS / Position
    ctx.strokeStyle = "#1e3050";
    ctx.beginPath(); ctx.moveTo(6, 108); ctx.lineTo(w-6, 108); ctx.stroke();

    var mgrs = pos.mgrs || (pos.lat ? coordToMGRSApprox(pos.lat, pos.lon) : "NO FIX");
    drawShadowText(ctx, mgrs, w/2, 120, "bold 7px monospace", "#60a0c0");

    // Callsign
    var call = data.callsign || data.operator || "UNKNOWN";
    drawShadowText(ctx, call, w/2, 136, "bold 8px monospace", "#38bdf8");
    
    drawHUDScanlines(ctx, w, h);
}

function drawOfflineOverlay(ctx, w, h) {
    ctx.fillStyle = "rgba(0,0,0,0.5)";
    ctx.fillRect(0, 24, w, h - 24);
    ctx.fillStyle = "#ef4444";
    ctx.font = "bold 10px monospace";
    ctx.textAlign = "center";
    ctx.fillText("OFFLINE", w/2, h/2 + 4);
}

// Rough MGRS approximation from lat/lon (just for display)
function coordToMGRSApprox(lat, lon) {
    if (lat === undefined || lon === undefined) return "NO FIX";
    var latStr = (Math.abs(lat)).toFixed(3) + (lat >= 0 ? "N" : "S");
    var lonStr = (Math.abs(lon)).toFixed(3) + (lon >= 0 ? "E" : "W");
    return latStr + " " + lonStr;
}

// ============================================================
//  TEAMMATE RANGE GRID
// ============================================================
function drawTeammateGrid(ctx, w, h, callsign, clients) {
    // Find teammate in client list
    var teammate = null;
    if (clients && Array.isArray(clients)) {
        for (var i = 0; i < clients.length; i++) {
            if (clients[i].callsign && clients[i].callsign.toUpperCase() === callsign.toUpperCase()) {
                teammate = clients[i];
                break;
            }
        }
    }

    var isTx = teammate && teammate.transmitting;
    var online = teammate && teammate.connected !== false;
    
    var theme = online ? (isTx ? "purple" : "purple") : "slate";
    drawHUDBackground(ctx, 0, 0, w, h, theme, online && isTx);

    // Header
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, online && isTx ? "#3b0764" : "#1a0a3a");
    grad.addColorStop(1, online && isTx ? "#120024" : "#2d1060");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = "#a78bfa";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("TEAMMATE", w/2, 14);

    if (!teammate) {
        ctx.fillStyle = "#a78bfa";
        ctx.font = "bold 10px monospace";
        ctx.textAlign = "center";
        ctx.fillText(callsign, w/2, 40);
        ctx.fillStyle = "#ef4444";
        ctx.font = "9px monospace";
        ctx.fillText("NOT FOUND", w/2, 58);
        ctx.fillStyle = "#555";
        ctx.font = "8px monospace";
        ctx.fillText("Check callsign", w/2, 72);
        drawHUDScanlines(ctx, w, h);
        return;
    }

    if (online && isTx) {
        applyNeonGlow(ctx, "#ef4444", 10);
        ctx.strokeStyle = "#ef4444";
        ctx.lineWidth = 3;
        ctx.strokeRect(3, 3, w-6, h-6);
        clearNeonGlow(ctx);
    } else if (online) {
        ctx.strokeStyle = "#a78bfa";
        ctx.lineWidth = 2;
        ctx.strokeRect(4, 4, w-8, h-8);
    } else {
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(4, 4, w-8, h-8);
    }

    // Callsign
    ctx.fillStyle = "#a78bfa";
    ctx.font = "bold 10px monospace";
    ctx.textAlign = "center";
    ctx.fillText(callsign.toUpperCase(), w/2, 38);

    // Status dot
    ctx.beginPath();
    ctx.arc(w/2 - 22, 52, 5, 0, Math.PI * 2);
    ctx.fillStyle = online ? "#22c55e" : "#ef4444";
    ctx.fill();
    ctx.fillStyle = online ? "#22c55e" : "#ef4444";
    ctx.font = "bold 8px monospace";
    ctx.textAlign = "left";
    ctx.fillText(online ? "ONLINE" : "OFFLINE", w/2 - 14, 56);

    // Divider
    ctx.strokeStyle = "#2a1560";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(6, 62); ctx.lineTo(w-6, 62); ctx.stroke();

    // Distance
    var dist = teammate.distance_m !== undefined ? teammate.distance_m : null;
    ctx.fillStyle = "#7090a0";
    ctx.font = "8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("DIST", 6, 76);
    
    ctx.textAlign = "right";
    var distStr = dist !== null ? formatDist(dist) : "---";
    drawShadowText(ctx, distStr, w-6, 76, "bold 10px monospace", dist !== null ? "#ffffff" : "#555", "right");

    // Bearing
    var bearing = teammate.bearing !== undefined ? teammate.bearing : null;
    ctx.fillStyle = "#7090a0";
    ctx.font = "8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("BRG", 6, 91);
    
    ctx.textAlign = "right";
    var brgStr = bearing !== null ? Math.round(bearing) + "°" : "---";
    drawShadowText(ctx, brgStr, w-6, 91, "bold 10px monospace", bearing !== null ? "#a78bfa" : "#555", "right");

    // Divider
    ctx.strokeStyle = "#2a1560";
    ctx.beginPath(); ctx.moveTo(6, 97); ctx.lineTo(w-6, 97); ctx.stroke();

    // TX state
    var tx = teammate.transmitting;
    ctx.fillStyle = "#7090a0";
    ctx.font = "8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("STATUS", 6, 110);
    ctx.fillStyle = tx ? "#ef4444" : "#22c55e";
    ctx.font = "bold 8px monospace";
    ctx.textAlign = "right";
    ctx.fillText(tx ? "● TX ACTIVE" : "○ STANDBY", w-6, 110);

    // Signal strength indicator (mini bars)
    var rssi = teammate.rssi !== undefined ? teammate.rssi : -120;
    var bars = Math.round(Math.max(0, Math.min(4, (rssi + 120) / 20)));
    drawSignalBars(ctx, 6, 120, bars, 4);

    ctx.fillStyle = "#556";
    ctx.font = "7px monospace";
    ctx.textAlign = "right";
    ctx.fillText(rssi + "dBm", w-6, 130);

    // Mini range ring (compass direction indicator)
    if (bearing !== null) {
        drawMiniRing(ctx, w-26, 122, 16, bearing);
    }

    drawHUDScanlines(ctx, w, h);
}

function formatDist(m) {
    if (m < 1000) return Math.round(m) + "m";
    return (m / 1000).toFixed(1) + "km";
}

function drawSignalBars(ctx, x, y, filled, total) {
    for (var i = 0; i < total; i++) {
        var bh = 4 + i * 2;
        ctx.fillStyle = i < filled ? "#a78bfa" : "#2a2040";
        ctx.fillRect(x + i * 7, y + (8 - bh), 5, bh);
    }
}

function drawMiniRing(ctx, cx, cy, r, bearing) {
    // Ring
    ctx.strokeStyle = "#2a1560";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();

    // N marker
    ctx.fillStyle = "#553";
    ctx.font = "6px monospace";
    ctx.textAlign = "center";
    ctx.fillText("N", cx, cy - r + 7);

    // Needle
    var rad = (bearing - 90) * Math.PI / 180;
    var nx = cx + Math.cos(rad) * (r - 3);
    var ny = cy + Math.sin(rad) * (r - 3);
    ctx.strokeStyle = "#a78bfa";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(nx, ny); ctx.stroke();

    // Center dot
    ctx.fillStyle = "#a78bfa";
    ctx.beginPath(); ctx.arc(cx, cy, 2, 0, Math.PI * 2); ctx.fill();
}

// ============================================================
//  OBJECTIVE COMPASS
// ============================================================
function drawCompass(ctx, w, h, navData) {
    var hasNav = !!(navData && navData.bearing !== null && navData.bearing !== undefined);
    drawHUDBackground(ctx, 0, 0, w, h, "green", hasNav);

    // Header
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, "#0a2a12");
    grad.addColorStop(1, "#0d4020");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = "#4ade80";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("OBJECTIVE", w/2, 14);

    if (!navData || navData.bearing === null || navData.bearing === undefined) {
        // No fix - draw static compass
        drawCompassRose(ctx, w/2, h/2 + 8, 48, null);
        ctx.fillStyle = "#666";
        ctx.font = "8px monospace";
        ctx.textAlign = "center";
        ctx.fillText("NO WAYPOINT", w/2, h - 10);
        drawHUDScanlines(ctx, w, h);
        return;
    }

    var bearing = navData.bearing;
    var dist    = navData.distance_m;
    var name    = navData.name || "OBJ";

    // Compass rose
    drawCompassRose(ctx, w/2, 82, 48, bearing);

    // Distance label
    drawShadowText(ctx, formatDist(dist), w/2, 142, "bold 8px monospace", "#4ade80");

    // Objective name
    drawShadowText(ctx, name.toUpperCase().substring(0, 12), w/2, 133, "7px monospace", "#a0d0b0");
    
    drawHUDScanlines(ctx, w, h);
}

function drawCompassRose(ctx, cx, cy, r, bearing) {
    // Outer ring glow
    var glow = ctx.createRadialGradient(cx, cy, r - 4, cx, cy, r + 6);
    glow.addColorStop(0, "rgba(74,222,128,0.15)");
    glow.addColorStop(1, "rgba(74,222,128,0)");
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(cx, cy, r + 6, 0, Math.PI * 2); ctx.fill();

    // Outer ring
    ctx.strokeStyle = "#1e4030";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();

    // Inner ring
    ctx.strokeStyle = "#0d2a1a";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, r - 8, 0, Math.PI * 2); ctx.stroke();

    // Cardinal points
    var cardinals = [
        { label: "N", deg: 0,   color: "#ef4444" },
        { label: "E", deg: 90,  color: "#4ade80" },
        { label: "S", deg: 180, color: "#4ade80" },
        { label: "W", deg: 270, color: "#4ade80" }
    ];
    cardinals.forEach(function(c) {
        var rad = (c.deg - 90) * Math.PI / 180;
        var tx = cx + Math.cos(rad) * (r - 6);
        var ty = cy + Math.sin(rad) * (r - 6);
        ctx.fillStyle = c.color;
        ctx.font = "bold 7px monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(c.label, tx, ty);
    });
    ctx.textBaseline = "alphabetic";

    // Tick marks
    for (var deg = 0; deg < 360; deg += 30) {
        var rad = (deg - 90) * Math.PI / 180;
        var isCardinal = (deg % 90 === 0);
        var inner = isCardinal ? r - 10 : r - 6;
        ctx.strokeStyle = isCardinal ? "#3a6050" : "#1e3025";
        ctx.lineWidth = isCardinal ? 2 : 1;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(rad) * inner, cy + Math.sin(rad) * inner);
        ctx.lineTo(cx + Math.cos(rad) * r, cy + Math.sin(rad) * r);
        ctx.stroke();
    }

    if (bearing === null || bearing === undefined) {
        // Draw static crosshairs
        ctx.strokeStyle = "#1e3025";
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(cx - r + 4, cy); ctx.lineTo(cx + r - 4, cy); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(cx, cy - r + 4); ctx.lineTo(cx, cy + r - 4); ctx.stroke();
        ctx.setLineDash([]);
        return;
    }

    // Needle
    var needleRad = (bearing - 90) * Math.PI / 180;
    var nx = cx + Math.cos(needleRad) * (r - 12);
    var ny = cy + Math.sin(needleRad) * (r - 12);

    // Needle shadow
    ctx.shadowColor = "#4ade80";
    ctx.shadowBlur = 8;

    ctx.strokeStyle = "#4ade80";
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(nx, ny); ctx.stroke();

    // Arrowhead
    var arLen = 8;
    var arW   = 0.4;
    var rev   = needleRad + Math.PI;
    ctx.fillStyle = "#4ade80";
    ctx.beginPath();
    ctx.moveTo(nx, ny);
    ctx.lineTo(
        nx + Math.cos(rev + arW) * arLen,
        ny + Math.sin(rev + arW) * arLen
    );
    ctx.lineTo(
        nx + Math.cos(rev - arW) * arLen,
        ny + Math.sin(rev - arW) * arLen
    );
    ctx.closePath();
    ctx.fill();

    ctx.shadowBlur = 0;

    // Bearing readout
    drawShadowText(ctx, Math.round(bearing) + "°", cx, cy + 5, "bold 9px monospace", "#4ade80");

    // Center dot
    ctx.fillStyle = "#ffffff";
    ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.fill();
}

// ============================================================
//  RADIO CHECK
// ============================================================
function drawRadioCheck(ctx, w, h, isFlashing) {
    drawHUDBackground(ctx, 0, 0, w, h, "red", isFlashing);

    // Animated background pulse
    if (isFlashing) {
        var flash = ctx.createRadialGradient(w/2, h/2, 0, w/2, h/2, w/2);
        flash.addColorStop(0, "rgba(239,68,68,0.45)");
        flash.addColorStop(1, "rgba(239,68,68,0)");
        ctx.fillStyle = flash;
        ctx.fillRect(0, 0, w, h);
    }

    // Header
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, isFlashing ? "#500a0a" : "#2a0a0a");
    grad.addColorStop(1, isFlashing ? "#180202" : "#400d0d");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = isFlashing ? "#ef4444" : "#f87171";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("RADIO CHECK", w/2, 14);

    if (isFlashing) {
        applyNeonGlow(ctx, "#ef4444", 12);
        ctx.strokeStyle = "#ef4444";
        ctx.lineWidth = 3;
        ctx.strokeRect(4, 4, w-8, h-8);
        clearNeonGlow(ctx);
    } else {
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, w-10, h-10);
    }

    // Waveform icon
    var mid = h / 2 + 8;
    var waves = [
        { r: 12, alpha: 0.9 },
        { r: 22, alpha: 0.55 },
        { r: 32, alpha: 0.3 },
        { r: 42, alpha: 0.15 }
    ];
    waves.forEach(function(wv) {
        ctx.strokeStyle = isFlashing
            ? "rgba(239,68,68," + wv.alpha + ")"
            : "rgba(248,113,113," + wv.alpha + ")";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(w/2, mid, wv.r, -Math.PI * 0.75, Math.PI * 0.75);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(w/2, mid, wv.r, Math.PI * 0.25, Math.PI * 1.75);
        ctx.stroke();
    });

    // Center transmit dot
    if (isFlashing) {
        applyNeonGlow(ctx, "#ef4444", 15);
    } else {
        applyNeonGlow(ctx, "#f87171", 8);
    }
    ctx.fillStyle = isFlashing ? "#ef4444" : "#f87171";
    ctx.beginPath();
    ctx.arc(w/2, mid, 5, 0, Math.PI * 2);
    ctx.fill();
    clearNeonGlow(ctx);

    // Label
    var labelColor = isFlashing ? "#ef4444" : "#888";
    var labelFont = isFlashing ? "bold 8px monospace" : "7px monospace";
    drawShadowText(ctx, isFlashing ? "TRANSMITTING" : "PRESS TO TEST", w/2, h - 10, labelFont, labelColor);

    drawHUDScanlines(ctx, w, h);
}

// ============================================================
//  KEY HANDLER
// ============================================================
function handleKeyDown(action, context, payload) {
    if (action === "com.tacnet.companion.radiocheck") {
        triggerRadioCheck(context);
    } else if (action === "com.tacnet.companion.nav") {
        // Cycle to next waypoint on key press
        var port = getClientPort(context);
        postJSON("http://127.0.0.1:" + port + "/api/nav/next", {}, function(r) {
            fetchAndRefresh();
        });
    } else if (action === "com.tacnet.companion.status") {
        // Force refresh
        fetchAndRefresh();
    } else if (action === "com.tacnet.companion.teammate") {
        // Force refresh teammates
        fetchAndRefresh();
    }
}

function triggerRadioCheck(context) {
    var port = getClientPort(context);
    radioCheckFlash[context] = 6;  // ~6 frames of flash
    updateContextState(context);   // immediate visual feedback

    postJSON("http://127.0.0.1:" + port + "/api/radiocheck", { tone: "silent_test" }, function(r) {
        // After response, fade flash
        setTimeout(function() {
            radioCheckFlash[context] = 0;
            updateContextState(context);
        }, 1800);
    });
}

