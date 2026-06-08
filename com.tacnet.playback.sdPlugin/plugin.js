// ============================================================
//  TacNet AAR Playback Deck – plugin.js
//  Polls radio_web.py /api/aar/playback/state (port 8890)
//  POSTs to /api/aar/playback/control for play/pause/seek/speed
//  Actions:
//    com.tacnet.playback.timeline  – jog-wheel + play/pause key
//    com.tacnet.playback.speed     – speed dial / key
//    com.tacnet.playback.session   – session loader key
//    com.tacnet.playback.bookmark  – bookmark/marker key
// ============================================================

var websocket  = null;
var pluginUUID = null;
var activeContexts = {};

var playbackState = {
    playing:    false,
    time:       0,
    duration:   0,
    speed:      1.0,
    session_id: "",
    file:       ""
};

var isFetching   = false;
var bookmarkFlash = {}; // context -> countdown

var SPEED_PRESETS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 4.0];

// ============================================================
//  STREAM DECK ENTRY
// ============================================================
function connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    pluginUUID = inUUID;
    websocket = new WebSocket("ws://127.0.0.1:" + inPort);

    websocket.onopen = function () {
        websocket.send(JSON.stringify({ "event": inRegisterEvent, "uuid": inUUID }));
    };

    websocket.onmessage = function (evt) {
        var obj     = JSON.parse(evt.data);
        var event   = obj["event"];
        var action  = obj["action"];
        var context = obj["context"];
        var payload = obj["payload"] || {};

        if (event === "willAppear") {
            activeContexts[context] = {
                action:   action,
                settings: payload.settings || {},
                device:   obj["device"],
                controller: payload.controller
            };
            updateContextState(context);
        }

        if (event === "willDisappear") {
            delete activeContexts[context];
            delete bookmarkFlash[context];
        }

        if (event === "didReceiveSettings") {
            if (activeContexts[context]) {
                activeContexts[context].settings = payload.settings || {};
                updateContextState(context);
            }
        }

        if (event === "keyDown" || event === "keyUp") {
            if (event === "keyDown") handleKeyDown(action, context, payload);
        }

        if (event === "dialRotate") {
            handleDialRotate(action, context, payload);
        }

        if (event === "dialPress") {
            handleDialPress(action, context, payload);
        }

        if (event === "touchTap") {
            handleTouchTap(action, context, payload);
        }
    };

    // Poll every 1 second for AAR state
    setInterval(fetchAndRefresh, 1000);
    fetchAndRefresh();
}

function connectElgatoStreamDeck(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo);
}

// ============================================================
//  FETCH
// ============================================================
function getServerPort(context) {
    if (context && activeContexts[context] && activeContexts[context].settings.server_port) {
        return parseInt(activeContexts[context].settings.server_port);
    }
    for (var c in activeContexts) {
        if (activeContexts[c].settings && activeContexts[c].settings.server_port) {
            return parseInt(activeContexts[c].settings.server_port);
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
    var port = getServerPort(null);

    fetchJSON("http://127.0.0.1:" + port + "/api/aar/playback/state", function (data) {
        isFetching = false;
        if (data) {
            playbackState.playing    = data.playing   || false;
            playbackState.time       = data.time      || 0;
            playbackState.duration   = data.duration  || 0;
            playbackState.speed      = data.speed     || 1.0;
            playbackState.session_id = data.session_id || "";
            playbackState.file       = data.file      || "";
        }
        for (var ctx in activeContexts) {
            updateContextState(ctx);
        }
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

function postControl(port, body) {
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "http://127.0.0.1:" + port + "/api/aar/playback/control", true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.timeout = 2000;
    xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) { fetchAndRefresh(); }
    };
    try { xhr.send(JSON.stringify(body)); } catch (e) {}
}

// ============================================================
//  REFRESH
// ============================================================
function updateContextState(context) {
    if (!activeContexts[context]) return;
    var action   = activeContexts[context].action;
    var settings = activeContexts[context].settings;
    var ctrl     = activeContexts[context].controller;

    var canvas = document.createElement("canvas");
    canvas.width  = 144;
    canvas.height = 144;
    var ctx = canvas.getContext("2d");

    if (action === "com.tacnet.playback.timeline") {
        drawTimeline(ctx, canvas.width, canvas.height, playbackState);
    } else if (action === "com.tacnet.playback.speed") {
        drawSpeed(ctx, canvas.width, canvas.height, playbackState);
    } else if (action === "com.tacnet.playback.session") {
        drawSession(ctx, canvas.width, canvas.height, playbackState, settings);
    } else if (action === "com.tacnet.playback.bookmark") {
        var flash = bookmarkFlash[context] || 0;
        drawBookmark(ctx, canvas.width, canvas.height, playbackState, flash > 0, settings);
        if (flash > 0) {
            bookmarkFlash[context] = flash - 1;
            if (flash - 1 > 0) {
                (function(c) { setTimeout(function() { updateContextState(c); }, 300); })(context);
            }
        }
    }

    var img = canvas.toDataURL("image/png").replace("data:image/png;base64,", "");
    setKeyImage(context, img);

    // Also update encoder touch display if it's a dial
    if (ctrl === "Encoder") {
        updateEncoderDisplay(context, action, settings);
    }
}

function setKeyImage(context, base64png) {
    if (!websocket) return;
    websocket.send(JSON.stringify({
        "event":   "setImage",
        "context": context,
        "payload": { "image": "data:image/png;base64," + base64png, "target": 0 }
    }));
}

function updateEncoderDisplay(context, action, settings) {
    if (!websocket) return;
    var title = "";
    var value = "";

    if (action === "com.tacnet.playback.timeline") {
        title = playbackState.playing ? "▶ PLAYING" : "⏸ PAUSED";
        value = formatTime(playbackState.time) + " / " + formatTime(playbackState.duration);
    } else if (action === "com.tacnet.playback.speed") {
        title = "SPEED";
        value = playbackState.speed.toFixed(2) + "x";
    }

    websocket.send(JSON.stringify({
        "event":   "setFeedback",
        "context": context,
        "payload": {
            "title": title,
            "value": value
        }
    }));
}

// ============================================================
//  DRAW – TIMELINE
// ============================================================
function drawTimeline(ctx, w, h, state) {
    var playing = state.playing;
    drawHUDBackground(ctx, 0, 0, w, h, "slate", playing);

    // Header gradient
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, playing ? "#064e3b" : "#0a1a2a");
    grad.addColorStop(1, playing ? "#022c22" : "#0d2a40");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = "#38bdf8";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("AAR TIMELINE", w/2, 14);

    // Active Border
    if (playing) {
        applyNeonGlow(ctx, "#22c55e", 12);
        ctx.strokeStyle = "#22c55e";
        ctx.lineWidth = 3;
        ctx.strokeRect(4, 4, w - 8, h - 8);
        clearNeonGlow(ctx);
    } else {
        ctx.strokeStyle = "#1e3050";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, w - 10, h - 10);
    }

    // Play/Pause indicator
    if (playing) {
        applyNeonGlow(ctx, "#22c55e", 10);
    }
    drawShadowText(ctx, playing ? "▶" : "⏸", w/2, 60, "bold 22px monospace", playing ? "#22c55e" : "#facc15");
    clearNeonGlow(ctx);

    // Time readout
    drawShadowText(ctx, formatTime(state.time), w/2, 80, "bold 11px monospace", "#ffffff");

    // Duration
    drawShadowText(ctx, "/ " + formatTime(state.duration), w/2, 92, "8px monospace", "#64748b");

    // Progress bar
    var dur = state.duration || 1;
    var prog = Math.max(0, Math.min(1, state.time / dur));
    var barX = 8, barY = 100, barW = w - 16, barH = 10;

    // Track
    ctx.fillStyle = "#1e2a38";
    ctx.beginPath();
    ctx.roundRect(barX, barY, barW, barH, 5);
    ctx.fill();

    // Fill
    if (prog > 0) {
        var fillGrad = ctx.createLinearGradient(barX, 0, barX + barW, 0);
        fillGrad.addColorStop(0, "#0ea5e9");
        fillGrad.addColorStop(1, "#38bdf8");
        ctx.fillStyle = fillGrad;
        ctx.beginPath();
        ctx.roundRect(barX, barY, Math.max(10, barW * prog), barH, 5);
        ctx.fill();
    }

    // Playhead thumb
    var thumbX = barX + barW * prog;
    ctx.fillStyle = "#ffffff";
    ctx.beginPath();
    ctx.arc(thumbX, barY + barH/2, 6, 0, Math.PI * 2);
    ctx.fill();

    // Speed label bottom
    drawShadowText(ctx, "SPD " + state.speed.toFixed(2) + "x", 8, 128, "7px monospace", "#64748b", "left");

    // Session name
    var sessName = state.session_id ? state.session_id.substring(0, 16) : "NO SESSION";
    drawShadowText(ctx, sessName, w - 8, 128, "7px monospace", state.session_id ? "#38bdf8" : "#475569", "right");

    drawHUDScanlines(ctx, w, h);
}

// ============================================================
//  DRAW – SPEED
// ============================================================
function drawSpeed(ctx, w, h, state) {
    var speed = state.speed || 1.0;
    var color = speedColor(speed);
    
    drawHUDBackground(ctx, 0, 0, w, h, "purple", false);

    // Header
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, "#1a0a30");
    grad.addColorStop(1, "#2d0a50");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = "#c084fc";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("SPEED", w/2, 14);

    // Border
    applyNeonGlow(ctx, color, 10);
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.strokeRect(4, 4, w - 8, h - 8);
    clearNeonGlow(ctx);

    // Speed value large
    applyNeonGlow(ctx, color, 8);
    drawShadowText(ctx, speed.toFixed(2), w/2, 70, "bold 28px monospace", color);
    clearNeonGlow(ctx);

    drawShadowText(ctx, "×", w/2, 88, "bold 13px monospace", "#8060a0");

    // Speed gauge arc
    drawSpeedArc(ctx, w/2, 100, 34, speed);

    // Preset labels
    drawShadowText(ctx, "0.25×  →  4.0×", 8, 140, "7px monospace", "#8060a0", "left");

    // Press to reset hint
    drawShadowText(ctx, "press=1×", w - 8, 140, "7px monospace", "#555566", "right");

    drawHUDScanlines(ctx, w, h);
}

function speedColor(speed) {
    if (speed < 1.0) return "#facc15";
    if (speed === 1.0) return "#22c55e";
    if (speed <= 2.0) return "#38bdf8";
    return "#ef4444";
}

function drawSpeedArc(ctx, cx, cy, r, speed) {
    var minS = 0.25, maxS = 4.0;
    var norm = Math.max(0, Math.min(1, (speed - minS) / (maxS - minS)));
    var startA = Math.PI * 0.75;
    var endA   = Math.PI * 2.25;
    var fillA  = startA + (endA - startA) * norm;

    // Track
    ctx.strokeStyle = "#1e1030";
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.arc(cx, cy, r, startA, endA);
    ctx.stroke();

    // Fill
    ctx.strokeStyle = speedColor(speed);
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.arc(cx, cy, r, startA, fillA);
    ctx.stroke();
}

// ============================================================
//  DRAW – SESSION
// ============================================================
function drawSession(ctx, w, h, state, settings) {
    var hasSession = !!(state.session_id && state.session_id.length > 0);
    drawHUDBackground(ctx, 0, 0, w, h, hasSession ? "green" : "slate", hasSession);

    // Header
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, hasSession ? "#0a200a" : "#1e293b");
    grad.addColorStop(1, hasSession ? "#0d3010" : "#0f172a");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = "#4ade80";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("AAR SESSION", w/2, 14);

    if (hasSession) {
        applyNeonGlow(ctx, "#4ade80", 10);
        ctx.strokeStyle = "#4ade80";
        ctx.lineWidth = 3;
        ctx.strokeRect(4, 4, w - 8, h - 8);
        clearNeonGlow(ctx);
    } else {
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, w - 10, h - 10);
    }

    // Folder icon area
    ctx.fillStyle = hasSession ? "rgba(34, 197, 94, 0.15)" : "rgba(100, 116, 139, 0.15)";
    ctx.beginPath();
    ctx.roundRect(16, 28, w - 32, 48, 6);
    ctx.fill();

    // Folder icon
    ctx.font = "24px monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = hasSession ? "#4ade80" : "#444";
    ctx.fillText(hasSession ? "📁" : "📂", w/2, 64);

    // Session ID
    if (hasSession) {
        var id = state.session_id;
        if (id.length > 14) id = id.substring(0, 14) + "…";
        drawShadowText(ctx, id, w/2, 86, "bold 7px monospace", "#4ade80");
    } else {
        drawShadowText(ctx, "NO SESSION", w/2, 86, "8px monospace", "#475569");
    }

    // Divider
    ctx.strokeStyle = hasSession ? "#1a3020" : "#334155";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(8, 94); ctx.lineTo(w-8, 94); ctx.stroke();

    // File info
    if (state.file) {
        var fname = state.file.split("/").pop().split("\\").pop();
        if (fname.length > 18) fname = fname.substring(0, 18) + "…";
        drawShadowText(ctx, fname, w/2, 108, "7px monospace", "#607060");
    }

    // Duration label
    if (state.duration > 0) {
        drawShadowText(ctx, "DUR " + formatTime(state.duration), w/2, 124, "bold 9px monospace", "#38bdf8");
    }

    // Press hint
    drawShadowText(ctx, "PRESS TO LOAD", w/2, 138, "7px monospace", hasSession ? "#2a4030" : "#475569");

    drawHUDScanlines(ctx, w, h);
}

// ============================================================
//  DRAW – BOOKMARK
// ============================================================
function drawBookmark(ctx, w, h, state, isFlashing, settings) {
    drawHUDBackground(ctx, 0, 0, w, h, "amber", isFlashing);

    if (isFlashing) {
        var pulse = ctx.createRadialGradient(w/2, h/2, 0, w/2, h/2, w/2);
        pulse.addColorStop(0, "rgba(250,204,21,0.35)");
        pulse.addColorStop(1, "rgba(250,204,21,0)");
        ctx.fillStyle = pulse;
        ctx.fillRect(0, 0, w, h);
    }

    // Header
    var grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, isFlashing ? "#451a03" : "#2a1a00");
    grad.addColorStop(1, isFlashing ? "#1c0b02" : "#3a2800");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, 22);

    ctx.fillStyle = isFlashing ? "#facc15" : "#fbbf24";
    ctx.font = "bold 9px monospace";
    ctx.textAlign = "center";
    ctx.fillText("BOOKMARK", w/2, 14);

    if (isFlashing) {
        applyNeonGlow(ctx, "#facc15", 12);
        ctx.strokeStyle = "#facc15";
        ctx.lineWidth = 3;
        ctx.strokeRect(4, 4, w - 8, h - 8);
        clearNeonGlow(ctx);
    } else {
        ctx.strokeStyle = "#3a2800";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, w - 10, h - 10);
    }

    // Bookmark flag icon
    if (isFlashing) {
        applyNeonGlow(ctx, "#facc15", 10);
    }
    ctx.font = "26px monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = isFlashing ? "#facc15" : "#a08020";
    ctx.fillText("🔖", w/2, 62);
    clearNeonGlow(ctx);

    // Label
    var label = (settings && settings.marker_label) || "HIGHLIGHT";
    drawShadowText(ctx, label.toUpperCase().substring(0, 10), w/2, 82, "bold 9px monospace", isFlashing ? "#facc15" : "#a08020");

    // Current time stamp
    var isTimeActive = state.playing || state.time > 0;
    drawShadowText(ctx, "@ " + formatTime(state.time), w/2, 102, "bold 12px monospace", isTimeActive ? "#ffffff" : "#475569");

    // Divider
    ctx.strokeStyle = "#3a2800";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(8, 110); ctx.lineTo(w-8, 110); ctx.stroke();

    drawShadowText(ctx, isFlashing ? "MARKING..." : "PRESS TO MARK", w/2, 124, "7px monospace", isFlashing ? "#facc15" : "#555562");

    // Session check
    if (!state.session_id) {
        drawShadowText(ctx, "NO SESSION", w/2, 138, "7px monospace", "#ef4444");
    }

    drawHUDScanlines(ctx, w, h);
}

// ============================================================
//  KEY / DIAL HANDLERS
// ============================================================
function handleKeyDown(action, context, payload) {
    var port = getServerPort(context);

    if (action === "com.tacnet.playback.timeline") {
        // Toggle play/pause
        postControl(port, { command: "playpause" });

    } else if (action === "com.tacnet.playback.speed") {
        // Reset to 1x
        postControl(port, { command: "set_speed", speed: 1.0 });

    } else if (action === "com.tacnet.playback.session") {
        // Request latest session load
        var settings = activeContexts[context] ? activeContexts[context].settings : {};
        var sid = settings.session_id || "";
        postControl(port, { command: "load_session", session_id: sid });

    } else if (action === "com.tacnet.playback.bookmark") {
        // Mark current position
        bookmarkFlash[context] = 5;
        updateContextState(context);
        var settings = activeContexts[context] ? activeContexts[context].settings : {};
        var label = (settings && settings.marker_label) || "HIGHLIGHT";
        postControl(port, { command: "bookmark", time: playbackState.time, label: label });
    }
}

function handleDialRotate(action, context, payload) {
    var port  = getServerPort(context);
    var ticks = payload.ticks || 0;   // positive = clockwise
    var settings = activeContexts[context] ? activeContexts[context].settings : {};

    if (action === "com.tacnet.playback.timeline") {
        var step = parseInt(settings.scrub_step) || 5;
        var delta = ticks * step;
        var newTime = Math.max(0, Math.min(playbackState.duration, playbackState.time + delta));
        postControl(port, { command: "seek", time: newTime });

    } else if (action === "com.tacnet.playback.speed") {
        // Find current index in presets
        var curIdx = findClosestSpeedIdx(playbackState.speed);
        var newIdx = Math.max(0, Math.min(SPEED_PRESETS.length - 1, curIdx + (ticks > 0 ? 1 : -1)));
        postControl(port, { command: "set_speed", speed: SPEED_PRESETS[newIdx] });
    }
}

function handleDialPress(action, context, payload) {
    var port = getServerPort(context);

    if (action === "com.tacnet.playback.timeline") {
        postControl(port, { command: "playpause" });
    } else if (action === "com.tacnet.playback.speed") {
        postControl(port, { command: "set_speed", speed: 1.0 });
    }
}

function handleTouchTap(action, context, payload) {
    var port = getServerPort(context);

    if (action === "com.tacnet.playback.timeline") {
        // Jump to start
        postControl(port, { command: "seek", time: 0 });
    } else if (action === "com.tacnet.playback.speed") {
        // Cycle speed presets
        var curIdx = findClosestSpeedIdx(playbackState.speed);
        var newIdx = (curIdx + 1) % SPEED_PRESETS.length;
        postControl(port, { command: "set_speed", speed: SPEED_PRESETS[newIdx] });
    }
}

function findClosestSpeedIdx(speed) {
    var best = 0, bestDiff = Math.abs(SPEED_PRESETS[0] - speed);
    for (var i = 1; i < SPEED_PRESETS.length; i++) {
        var d = Math.abs(SPEED_PRESETS[i] - speed);
        if (d < bestDiff) { bestDiff = d; best = i; }
    }
    return best;
}

// ============================================================
//  UTILITIES & HUD UTILITIES
// ============================================================
function formatTime(seconds) {
    if (!seconds || isNaN(seconds)) return "0:00";
    var s = Math.floor(seconds);
    var m = Math.floor(s / 60);
    var h = Math.floor(m / 60);
    m = m % 60;
    s = s % 60;
    if (h > 0) {
         return h + ":" + pad2(m) + ":" + pad2(s);
    }
    return m + ":" + pad2(s);
}

function pad2(n) {
    return n < 10 ? "0" + n : "" + n;
}

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

