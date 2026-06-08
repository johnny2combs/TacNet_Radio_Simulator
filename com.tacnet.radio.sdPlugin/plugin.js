var websocket = null;
var pluginUUID = null;
var apiPort = 8895; // Default port
var clientState = null;
var activeContexts = {}; // context -> action
var animationFrame = 0;
var wasJammed = false;
var devices = {}; // device -> deviceInfo
var lastProfileSwitchTime = 0;

function connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    pluginUUID = inUUID;
    websocket = new WebSocket("ws://127.0.0.1:" + inPort);
    
    websocket.onopen = function () {
        var json = {
            "event": inRegisterEvent,
            "uuid": inUUID
        };
        websocket.send(JSON.stringify(json));
    };
    
    websocket.onmessage = function (evt) {
        var jsonObj = JSON.parse(evt.data);
        var event = jsonObj['event'];
        var action = jsonObj['action'];
        var context = jsonObj['context'];
        var device = jsonObj['device'];
        var payload = jsonObj['payload'] || {};
        
        if (event === "willAppear") {
            activeContexts[context] = {
                action: action,
                settings: payload.settings || {},
                device: device,
                controller: payload.controller
            };
            updateContextState(context);
        }
        
        if (event === "willDisappear") {
            delete activeContexts[context];
        }
        
        if (event === "didReceiveSettings") {
            if (activeContexts[context]) {
                activeContexts[context].settings = payload.settings || {};
                updateContextState(context);
            }
        }
        
        if (event === "keyDown" || event === "keyUp") {
            handleKeyEvent(event, action, context, payload);
        }
        
        if (event === "dialPress" || event === "dialRotate" || event === "touchTap") {
            handleDialEvent(event, action, context, payload);
        }
        
        if (event === "deviceDidConnect") {
            devices[device] = jsonObj['deviceInfo'];
            autoSwitchProfile(device, jsonObj['deviceInfo']);
        }
    };
}

function connectElgatoStreamDeck(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo) {
    connectElgatoStreamDeckSocket(inPort, inUUID, inRegisterEvent, inInfo, inActionInfo);
}

function autoSwitchProfile(device, deviceInfo) {
    var profileName = "TacNet Grid";
    if (deviceInfo.type === 2) { // XL
        profileName = "TacNet Grid XL";
    } else if (deviceInfo.type === 7) { // Plus
        profileName = "TacNet Plus";
    }
    
    if (websocket) {
        var json = {
            "event": "switchToProfile",
            "context": pluginUUID,
            "device": device,
            "payload": {
                "profile": profileName
            }
        };
        websocket.send(JSON.stringify(json));
    }
}

var isFetching = false;

function fetchClientState() {
    if (isFetching) return;
    isFetching = true;
    
    fetch("http://127.0.0.1:" + apiPort + "/api/status")
        .then(response => response.json())
        .then(data => {
            clientState = data;
            
            // Auto Profile Switching on Jamming transition
            if (clientState) {
                var isJammed = clientState.jammed;
                if (isJammed && !wasJammed) {
                    wasJammed = true;
                    triggerAutoProfileSwitch();
                } else if (!isJammed) {
                    wasJammed = false;
                }
            }
            
            // Update all active contexts
            for (var context in activeContexts) {
                updateContextState(context);
            }
            isFetching = false;
        })
        .catch(err => {
            clientState = null; // Mark as disconnected
            for (var context in activeContexts) {
                updateContextState(context);
            }
            isFetching = false;
        });
}

// Poll state every 200ms
setInterval(fetchClientState, 200);

var lastRenderState = {};

function updateContextState(context) {
    var ctxInfo = activeContexts[context];
    if (!ctxInfo) return;
    var settings = ctxInfo.settings || {};
    
    var currentVisualState = {
        connected: clientState !== null,
        action: ctxInfo.action,
        settings: settings
    };
    
    if (clientState) {
        var action = ctxInfo.action;
        if (action === "com.tacnet.radio.ptt") {
            currentVisualState.tx_active = clientState.tx_active;
            currentVisualState.tx_level = clientState.tx_level;
            currentVisualState.callsign = clientState.callsign;
            currentVisualState.current_channel = clientState.current_channel;
        } else if (action === "com.tacnet.radio.status") {
            currentVisualState.rx_active = clientState.rx_active;
            currentVisualState.rx_level = clientState.rx_level;
            currentVisualState.rx_callsign = clientState.rx_callsign;
            currentVisualState.frequency = clientState.frequency;
            currentVisualState.signal_strength = clientState.signal_strength;
        } else if (action === "com.tacnet.radio.channel") {
            var targetCh = (ctxInfo.settings.channel !== undefined) ? ctxInfo.settings.channel : 1;
            currentVisualState.current_channel = clientState.current_channel;
            currentVisualState.active_channels_match = clientState.active_channels && clientState.active_channels.includes(targetCh);
            currentVisualState.channel_name = clientState.channel_name;
            currentVisualState.channel_name_custom = clientState.channels ? clientState.channels[targetCh] : null;
        } else if (action === "com.tacnet.radio.volume" || action === "com.tacnet.radio.volup" || action === "com.tacnet.radio.voldown") {
            currentVisualState.volume = clientState.volume;
        } else if (action === "com.tacnet.radio.vox") {
            currentVisualState.vox = clientState.vox;
        } else if (action === "com.tacnet.radio.gps") {
            currentVisualState.mgrs = clientState.mgrs;
        } else if (action === "com.tacnet.radio.roster") {
            currentVisualState.roster_count = clientState.roster_count;
        } else if (action === "com.tacnet.radio.comsec") {
            currentVisualState.encryption = clientState.encryption;
        } else if (action === "com.tacnet.radio.signal") {
            currentVisualState.signal_strength = clientState.signal_strength;
        } else if (action === "com.tacnet.radio.ew") {
            currentVisualState.jammed = clientState.jammed;
        } else if (action === "com.tacnet.radio.member") {
            var targetCs = (ctxInfo.settings.callsign || "").trim().toUpperCase();
            var targetMem = clientState.members ? clientState.members.find(m => m && m.callsign === targetCs) : null;
            if (targetMem) {
                currentVisualState.member_exists = true;
                currentVisualState.member_active = targetMem.active;
                currentVisualState.member_stale = targetMem.stale;
                currentVisualState.member_distance = targetMem.distance;
            } else {
                currentVisualState.member_exists = false;
            }
        } else if (action === "com.tacnet.radio.lcd") {
            currentVisualState.callsign = clientState.callsign;
            currentVisualState.current_channel = clientState.current_channel;
            currentVisualState.channel_name = clientState.channel_name;
            currentVisualState.frequency = clientState.frequency;
            currentVisualState.tx_active = clientState.tx_active;
            currentVisualState.rx_active = clientState.rx_active;
            currentVisualState.rx_callsign = clientState.rx_callsign;
            currentVisualState.encryption = clientState.encryption;
            currentVisualState.signal_strength = clientState.signal_strength;
        }
    }
    
    var isAnimating = false;
    if (clientState) {
        if (ctxInfo.action === "com.tacnet.radio.ptt" && clientState.tx_active) {
            isAnimating = true;
        } else if (ctxInfo.action === "com.tacnet.radio.status" && (clientState.rx_active || clientState.jammed)) {
            isAnimating = true;
        } else if (ctxInfo.action === "com.tacnet.radio.channel") {
            var targetCh = (ctxInfo.settings.channel !== undefined) ? ctxInfo.settings.channel : 1;
            var hasActivity = clientState.active_channels && clientState.active_channels.includes(targetCh);
            var isActive = (clientState.current_channel === targetCh);
            if (hasActivity && !isActive) {
                isAnimating = true;
            }
        } else if (ctxInfo.action === "com.tacnet.radio.member") {
            var targetCs = (ctxInfo.settings.callsign || "").trim().toUpperCase();
            var targetMem = clientState.members ? clientState.members.find(m => m && m.callsign === targetCs) : null;
            if (targetMem && targetMem.active) {
                isAnimating = true;
            }
        } else if (ctxInfo.action === "com.tacnet.radio.lcd") {
            if (clientState.tx_active || clientState.rx_active || clientState.jammed) {
                isAnimating = true;
            }
        }
    }
    
    if (isAnimating) {
        currentVisualState.animationFrame = animationFrame;
    }
    
    var stateString = JSON.stringify(currentVisualState);
    if (lastRenderState[context] === stateString) {
        return; // Skip redundant rendering
    }
    lastRenderState[context] = stateString;
    
    // Update Touch Strip feedback if Encoder
    if (ctxInfo.controller === "Encoder") {
        updateEncoderFeedback(context, ctxInfo);
    }
    
    var canvas = document.createElement("canvas");
    canvas.width = 144;
    canvas.height = 144;
    var ctx = canvas.getContext("2d");
    
    // Draw background with radial screen glow
    var isConnected = clientState !== null;
    drawHUDBackground(ctx, 0, 0, 144, 144, isConnected ? "slate" : "red", false);
    
    if (!clientState) {
        // Disconnected State
        drawHUDBackground(ctx, 5, 5, 134, 134, "red", true);
        applyNeonGlow(ctx, "#ef4444", 12);
        ctx.strokeStyle = "#ef4444";
        ctx.lineWidth = 4;
        ctx.strokeRect(4, 4, 136, 136);
        clearNeonGlow(ctx);
        
        ctx.fillStyle = "#ef4444";
        ctx.font = "bold 16px Arial";
        ctx.textAlign = "center";
        ctx.fillText("DISCONNECTED", 72, 60);
        ctx.fillStyle = "#64748b";
        ctx.font = "12px Arial";
        ctx.fillText("Check TacNet Client", 72, 90);
        setImage(context, canvas.toDataURL());
        return;
    }
    
    // Render graphics based on action type
    if (ctxInfo.action === "com.tacnet.radio.ptt") {
        var isTX = clientState.tx_active;
        drawHUDBackground(ctx, 5, 5, 134, 134, isTX ? "red" : "slate", isTX);
        
        if (isTX) {
            applyNeonGlow(ctx, "#ef4444", 15);
            ctx.strokeStyle = "#ef4444";
            ctx.lineWidth = 4;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
            
            applyNeonGlow(ctx, "#ef4444", 10);
            ctx.fillStyle = "#ef4444";
            ctx.font = "bold 24px Arial";
            ctx.textAlign = "center";
            ctx.fillText("TX", 72, 40);
            clearNeonGlow(ctx);
            
            ctx.fillStyle = "#ffffff";
            ctx.font = "bold 14px Arial";
            ctx.fillText(clientState.callsign, 72, 65);
            
            drawGlowingWave(ctx, 105, clientState.tx_level, "#f87171");
        } else {
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);

            ctx.fillStyle = "#ffffff";
            ctx.font = "bold 24px Arial";
            ctx.textAlign = "center";
            ctx.fillText("PTT", 72, 60);
            
            ctx.fillStyle = "#38bdf8";
            ctx.font = "14px Arial";
            ctx.fillText(clientState.callsign, 72, 95);
            
            drawShadowText(ctx, "CH " + clientState.current_channel, 72, 115, "10px monospace", "#ffffff");
        }
        
    } else if (ctxInfo.action === "com.tacnet.radio.status") {
        var isRX = clientState.rx_active;
        var isJammed = clientState.jammed;
        var theme = isJammed ? "red" : (isRX ? "green" : "slate");
        drawHUDBackground(ctx, 5, 5, 134, 134, theme, isJammed || isRX);
        
        if (isJammed) {
            var isBlink = Math.floor(animationFrame / 2) % 2 === 0;
            var color = isBlink ? "#ef4444" : "#b91c1c";
            
            applyNeonGlow(ctx, "#ef4444", 12);
            ctx.strokeStyle = color;
            ctx.lineWidth = 4;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
            
            applyNeonGlow(ctx, "#ef4444", 10);
            ctx.fillStyle = "#ef4444";
            ctx.font = "bold 20px Arial";
            ctx.textAlign = "center";
            ctx.fillText("JAMMED", 72, 45);
            clearNeonGlow(ctx);
            
            drawShadowText(ctx, clientState.frequency, 72, 80, "14px monospace", "#ffffff");
            
            drawGlowingWave(ctx, 110, clientState.rx_level || 0.4, "#ef4444");
        } else if (isRX) {
            applyNeonGlow(ctx, "#22c55e", 12);
            ctx.strokeStyle = "#22c55e";
            ctx.lineWidth = 4;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
            
            applyNeonGlow(ctx, "#22c55e", 10);
            ctx.fillStyle = "#22c55e";
            ctx.font = "bold 20px Arial";
            ctx.textAlign = "center";
            ctx.fillText("RX ACTIVE", 72, 38);
            clearNeonGlow(ctx);
            
            ctx.fillStyle = "#ffffff";
            ctx.font = "bold 16px Arial";
            ctx.fillText(clientState.rx_callsign, 72, 65);
            
            drawGlowingWave(ctx, 105, clientState.rx_level, "#4ade80");
        } else {
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);

            ctx.fillStyle = "#ffffff";
            ctx.font = "bold 20px Arial";
            ctx.textAlign = "center";
            ctx.fillText("STANDBY", 72, 45);
            
            drawShadowText(ctx, clientState.frequency, 72, 80, "14px monospace", "#38bdf8");
            
            var bars = Math.round(clientState.signal_strength * 5);
            ctx.fillStyle = "#22c55e";
            for (var i = 0; i < 5; i++) {
                var h = (i + 1) * 4;
                ctx.fillRect(40 + (i * 12), 120 - h, 8, h);
                if (i >= bars) {
                    ctx.fillStyle = "rgba(255,255,255,0.15)";
                    ctx.fillRect(40 + (i * 12), 120 - h, 8, h);
                    ctx.fillStyle = "#22c55e";
                }
            }
        }
        
    } else if (ctxInfo.action === "com.tacnet.radio.channel") {
        var targetCh = (ctxInfo.settings.channel !== undefined) ? ctxInfo.settings.channel : 1;
        var isActive = (clientState.current_channel === targetCh);
        var hasActivity = clientState.active_channels && clientState.active_channels.includes(targetCh);
        
        var theme = isActive ? "yellow" : (hasActivity ? "green" : "slate");
        drawHUDBackground(ctx, 5, 5, 134, 134, theme, isActive || hasActivity);
        
        if (isActive) {
            applyNeonGlow(ctx, "#eab308", 12);
            ctx.strokeStyle = "#eab308";
            ctx.lineWidth = 4;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
        } else if (hasActivity) {
            var isBlink = Math.floor(animationFrame / 3) % 2 === 0;
            ctx.strokeStyle = isBlink ? "#22c55e" : "#064e3b";
            ctx.lineWidth = 4;
            ctx.strokeRect(4, 4, 136, 136);
        } else {
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);
        }
        
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 16px Arial";
        ctx.textAlign = "center";
        ctx.fillText("CH " + targetCh, 72, 40);
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 12px Arial";
        var nameText = (clientState.channels && clientState.channels[targetCh]) ? clientState.channels[targetCh] : 
                       (clientState.current_channel === targetCh ? clientState.channel_name : ("PRESET " + targetCh));
        ctx.fillText(nameText.substring(0, 15), 72, 75);
        
        drawShadowText(ctx, isActive ? "ACTIVE" : (hasActivity ? "TRAFFIC" : "SELECT"), 72, 110, "10px monospace", "#64748b");
        
        if (hasActivity && !isActive) {
            applyNeonGlow(ctx, "#22c55e", 10);
            ctx.fillStyle = "#22c55e";
            ctx.beginPath();
            ctx.arc(20, 20, 6, 0, 2 * Math.PI);
            ctx.fill();
            clearNeonGlow(ctx);
        }
        
    } else if (ctxInfo.action === "com.tacnet.radio.volume") {
        var volPercent = Math.round(clientState.volume * 100);
        var isMute = (volPercent === 0);
        
        drawHUDBackground(ctx, 5, 5, 134, 134, isMute ? "red" : "slate", isMute);
        
        if (isMute) {
            applyNeonGlow(ctx, "#ef4444", 12);
            ctx.strokeStyle = "#ef4444";
            ctx.lineWidth = 3;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
        } else {
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);
        }
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText(isMute ? "MUTED" : "VOLUME", 72, 45);
        
        drawShadowText(ctx, volPercent + "%", 72, 85, "bold 24px monospace", "#ffffff");
        
        ctx.fillStyle = "#64748b";
        ctx.font = "10px Arial";
        ctx.fillText("Press to Mute", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.volup") {
        var volPercent = Math.round(clientState.volume * 100);
        drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
        
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, 134, 134);
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("VOL UP", 72, 45);
        
        drawShadowText(ctx, volPercent + "%", 72, 85, "bold 24px monospace", "#ffffff");
        
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 20px Arial";
        ctx.fillText("+", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.voldown") {
        var volPercent = Math.round(clientState.volume * 100);
        drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
        
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, 134, 134);
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("VOL DOWN", 72, 45);
        
        drawShadowText(ctx, volPercent + "%", 72, 85, "bold 24px monospace", "#ffffff");
        
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 20px Arial";
        ctx.fillText("-", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.vox") {
        var isVox = clientState.vox;
        drawHUDBackground(ctx, 5, 5, 134, 134, isVox ? "green" : "slate", isVox);
        
        if (isVox) {
            applyNeonGlow(ctx, "#22c55e", 12);
            ctx.strokeStyle = "#22c55e";
            ctx.lineWidth = 3;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
        } else {
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);
        }
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 20px Arial";
        ctx.textAlign = "center";
        ctx.fillText("VOX", 72, 50);
        
        if (isVox) {
            applyNeonGlow(ctx, "#22c55e", 10);
            drawShadowText(ctx, "ON", 72, 95, "bold 24px monospace", "#22c55e");
            clearNeonGlow(ctx);
        } else {
            drawShadowText(ctx, "OFF", 72, 95, "bold 24px monospace", "#64748b");
        }
        
    } else if (ctxInfo.action === "com.tacnet.radio.gps") {
        drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
        
        ctx.strokeStyle = "#38bdf8";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, 134, 134);
        
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 14px Arial";
        ctx.textAlign = "center";
        ctx.fillText("GPS POSITION", 72, 35);
        
        var mgrs = clientState.mgrs || "00A AA 0000 0000";
        var parts = mgrs.split(" ");
        if (parts.length >= 4) {
            drawShadowText(ctx, parts[0] + " " + parts[1], 72, 65, "bold 11px monospace", "#ffffff");
            drawShadowText(ctx, parts[2] + " " + parts[3], 72, 85, "bold 11px monospace", "#ffffff");
        } else {
            drawShadowText(ctx, mgrs, 72, 75, "bold 11px monospace", "#ffffff");
        }
        
        ctx.fillStyle = "#22c55e";
        ctx.font = "bold 10px Arial";
        ctx.fillText("● 3D FIX", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.roster") {
        drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
        
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, 134, 134);
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("ROSTER", 72, 45);
        
        drawShadowText(ctx, (clientState.roster_count || 0) + " OP", 72, 85, "bold 24px monospace", "#a855f7");
        
        ctx.fillStyle = "#64748b";
        ctx.font = "10px Arial";
        ctx.fillText("Show Operators", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.comsec") {
        var isEnc = (clientState.encryption === "AES-256");
        drawHUDBackground(ctx, 5, 5, 134, 134, isEnc ? "green" : "red", isEnc);
        
        if (isEnc) {
            applyNeonGlow(ctx, "#22c55e", 12);
            ctx.strokeStyle = "#22c55e";
            ctx.lineWidth = 3;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
        } else {
            applyNeonGlow(ctx, "#ef4444", 12);
            ctx.strokeStyle = "#ef4444";
            ctx.lineWidth = 3;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
        }
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("COMSEC", 72, 45);
        
        if (isEnc) {
            applyNeonGlow(ctx, "#22c55e", 10);
            drawShadowText(ctx, "🔐 SECURE", 72, 85, "bold 20px Arial", "#22c55e");
            clearNeonGlow(ctx);
        } else {
            applyNeonGlow(ctx, "#ef4444", 10);
            drawShadowText(ctx, "🔓 PLAIN", 72, 85, "bold 20px Arial", "#ef4444");
            clearNeonGlow(ctx);
        }
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "10px Arial";
        ctx.fillText("Press to Toggle", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.signal") {
        drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
        
        ctx.strokeStyle = "#38bdf8";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, 134, 134);
        
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("SIGNAL SNR", 72, 40);
        
        var snr = Math.round(clientState.signal_strength * 100);
        drawShadowText(ctx, snr + "%", 72, 75, "bold 24px monospace", "#ffffff");
        
        var bars = Math.round(clientState.signal_strength * 5);
        ctx.fillStyle = "#22c55e";
        for (var i = 0; i < 5; i++) {
            var h = (i + 1) * 4;
            ctx.fillRect(40 + (i * 12), 120 - h, 8, h);
            if (i >= bars) {
                ctx.fillStyle = "rgba(255,255,255,0.15)";
                ctx.fillRect(40 + (i * 12), 120 - h, 8, h);
                ctx.fillStyle = "#22c55e";
            }
        }
        
    } else if (ctxInfo.action === "com.tacnet.radio.ew") {
        var isJammed = clientState.jammed;
        var theme = isJammed ? "red" : "slate";
        drawHUDBackground(ctx, 5, 5, 134, 134, theme, isJammed);
        
        var isBlink = isJammed && (Math.floor(Date.now() / 500) % 2 === 0);
        var alertColor = isBlink ? "#ef4444" : "#b91c1c";
        
        if (isJammed) {
            applyNeonGlow(ctx, "#ef4444", 12);
            ctx.strokeStyle = alertColor;
            ctx.lineWidth = 3;
            ctx.strokeRect(4, 4, 136, 136);
            clearNeonGlow(ctx);
        } else {
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);
        }
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("EW STATUS", 72, 45);
        
        if (isJammed) {
            applyNeonGlow(ctx, "#ef4444", 10);
            drawShadowText(ctx, "⚠️ JAMMED", 72, 85, "bold 20px Arial", "#ef4444");
            clearNeonGlow(ctx);
        } else {
            drawShadowText(ctx, "🛡️ CLEAR", 72, 85, "bold 20px Arial", "#22c55e");
        }
        
        ctx.fillStyle = isJammed ? "#ffffff" : "#64748b";
        ctx.font = "10px Arial";
        ctx.fillText(isJammed ? "RF DENIAL ACTIVE" : "NET CLEAR", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.map") {
        drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
        
        ctx.strokeStyle = "#475569";
        ctx.lineWidth = 2;
        ctx.strokeRect(5, 5, 134, 134);
        
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 18px Arial";
        ctx.textAlign = "center";
        ctx.fillText("MAP VIEW", 72, 45);
        
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 24px Arial";
        ctx.fillText("🧭 RADAR", 72, 85);
        
        ctx.fillStyle = "#64748b";
        ctx.font = "10px Arial";
        ctx.fillText("Toggle Plotter", 72, 115);
        
    } else if (ctxInfo.action === "com.tacnet.radio.member") {
        var targetCs = (ctxInfo.settings.callsign || "").trim().toUpperCase();
        
        if (!targetCs) {
            drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(5, 5, 134, 134);
            
            ctx.fillStyle = "#94a3b8";
            ctx.font = "bold 14px Arial";
            ctx.textAlign = "center";
            ctx.fillText("NO CALLSIGN", 72, 60);
            ctx.font = "11px Arial";
            ctx.fillText("Set in settings", 72, 85);
        } else {
            var targetMem = null;
            if (clientState && clientState.members) {
                targetMem = clientState.members.find(m => m && m.callsign === targetCs);
            }
            
            if (!targetMem) {
                drawHUDBackground(ctx, 5, 5, 134, 134, "slate", false);
                ctx.strokeStyle = "#475569";
                ctx.lineWidth = 2;
                ctx.strokeRect(5, 5, 134, 134);
                
                ctx.fillStyle = "#334155";
                ctx.beginPath();
                ctx.arc(72, 55, 16, 0, 2*Math.PI);
                ctx.fill();
                ctx.beginPath();
                ctx.arc(72, 105, 24, Math.PI, 2*Math.PI);
                ctx.fill();
                
                ctx.fillStyle = "#94a3b8";
                ctx.font = "bold 14px Arial";
                ctx.textAlign = "center";
                ctx.fillText(targetCs, 72, 120);
                ctx.font = "11px Arial";
                ctx.fillText("OFFLINE", 72, 28);
            } else if (targetMem.active) {
                var blink = Math.floor(animationFrame / 2) % 2 === 0;
                drawHUDBackground(ctx, 5, 5, 134, 134, "green", true);
                
                applyNeonGlow(ctx, "#22c55e", 12);
                ctx.strokeStyle = "#22c55e";
                ctx.lineWidth = 4;
                ctx.strokeRect(4, 4, 136, 136);
                clearNeonGlow(ctx);
                
                applyNeonGlow(ctx, "#22c55e", 10);
                ctx.fillStyle = "#22c55e";
                ctx.font = "bold 15px Arial";
                ctx.textAlign = "center";
                ctx.fillText(targetCs, 72, 50);
                ctx.font = "bold 16px Arial";
                ctx.fillText("TX ACTIVE", 72, 85);
                clearNeonGlow(ctx);
                
                var distStr = targetMem.distance >= 0 ? (targetMem.distance < 1.0 ? Math.round(targetMem.distance * 1000) + " M" : targetMem.distance.toFixed(1) + " KM") : "-- KM";
                drawShadowText(ctx, distStr, 72, 115, "11px monospace", "#ffffff");
            } else if (targetMem.stale) {
                drawHUDBackground(ctx, 5, 5, 134, 134, "red", true);
                
                applyNeonGlow(ctx, "#ef4444", 12);
                ctx.strokeStyle = "#ef4444";
                ctx.lineWidth = 3;
                ctx.strokeRect(4, 4, 136, 136);
                clearNeonGlow(ctx);
                
                applyNeonGlow(ctx, "#ef4444", 10);
                ctx.fillStyle = "#ef4444";
                ctx.font = "bold 14px Arial";
                ctx.textAlign = "center";
                ctx.fillText(targetCs, 72, 50);
                ctx.fillText("STALE", 72, 85);
                clearNeonGlow(ctx);
                
                var distStr = targetMem.distance >= 0 ? (targetMem.distance < 1.0 ? Math.round(targetMem.distance * 1000) + " M" : targetMem.distance.toFixed(1) + " KM") : "-- KM";
                drawShadowText(ctx, distStr, 72, 115, "11px monospace", "#ffffff");
            } else {
                drawHUDBackground(ctx, 5, 5, 134, 134, "green", false);
                
                ctx.strokeStyle = "#22c55e";
                ctx.lineWidth = 2;
                ctx.strokeRect(5, 5, 134, 134);
                
                ctx.fillStyle = "rgba(30, 41, 59, 0.6)";
                ctx.beginPath();
                ctx.arc(72, 55, 16, 0, 2*Math.PI);
                ctx.fill();
                ctx.beginPath();
                ctx.arc(72, 105, 24, Math.PI, 2*Math.PI);
                ctx.fill();
                
                ctx.fillStyle = "#ffffff";
                ctx.font = "bold 14px Arial";
                ctx.textAlign = "center";
                ctx.fillText(targetCs, 72, 120);
                
                var distStr = targetMem.distance >= 0 ? (targetMem.distance < 1.0 ? Math.round(targetMem.distance * 1000) + " M" : targetMem.distance.toFixed(1) + " KM") : "-- KM";
                drawShadowText(ctx, distStr, 72, 28, "bold 11px monospace", "#22c55e");
            }
        }
        
    } else if (ctxInfo.action === "com.tacnet.radio.lcd") {
        var theme = settings.lcd_theme || "classic";
        var bg_color = "#22c55e"; 
        var text_color = "#000000";
        var border_color = "#15803d";
        
        if (theme === "amber") {
            bg_color = "#f59e0b";
            text_color = "#000000";
            border_color = "#b45309";
        } else if (theme === "blue") {
            bg_color = "#0284c7";
            text_color = "#ffffff";
            border_color = "#0369a1";
        } else if (theme === "dark") {
            bg_color = "#0f172a";
            text_color = "#f8fafc";
            border_color = "#1e293b";
        }
        
        ctx.fillStyle = bg_color;
        ctx.fillRect(0, 0, 144, 144);
        
        ctx.strokeStyle = border_color;
        ctx.lineWidth = 4;
        ctx.strokeRect(2, 2, 140, 140);
        
        // Draw LCD Grid micro-lines
        ctx.fillStyle = "rgba(0, 0, 0, 0.03)";
        for (var x = 0; x < 144; x += 4) {
            ctx.fillRect(x, 0, 1, 144);
        }
        for (var y = 0; y < 144; y += 4) {
            ctx.fillRect(0, y, 144, 1);
        }
        
        ctx.fillStyle = text_color;
        ctx.textAlign = "center";
        
        // Callsign / Status Row
        ctx.font = "bold 13px monospace";
        ctx.fillText(clientState.callsign || "TACNET", 72, 22);
        
        // Channel display
        ctx.font = "bold 32px monospace";
        ctx.fillText("CH " + (clientState.current_channel !== undefined ? clientState.current_channel : 1), 72, 60);
        
        // Frequency/Net name
        ctx.font = "bold 11px monospace";
        var netName = (clientState.channel_name || "GUARD").toString();
        ctx.fillText(netName.substring(0, 16), 72, 80);
        
        // RX/TX status row
        var rxTxStr = "STBY";
        if (clientState.tx_active) {
            rxTxStr = "■ TX ACTIVE ■";
            ctx.fillStyle = theme === "dark" ? "#ef4444" : "#450a0a";
        } else if (clientState.rx_active) {
            rxTxStr = "▶ RX: " + (clientState.rx_callsign || "");
            ctx.fillStyle = theme === "dark" ? "#22c55e" : "#064e3b";
        } else {
            ctx.fillStyle = text_color;
        }
        ctx.font = "bold 12px monospace";
        ctx.fillText(rxTxStr, 72, 102);
        
        // Footer: COMSEC lock & Signal bars
        ctx.fillStyle = text_color;
        ctx.font = "bold 11px monospace";
        var isEnc = (clientState.encryption === "AES-256");
        var comsecIcon = isEnc ? "🔐" : "🔓";
        ctx.textAlign = "left";
        ctx.fillText(comsecIcon + " SEC", 12, 128);
        
        // Draw Signal bars
        ctx.textAlign = "right";
        var sigStrength = clientState.signal_strength !== undefined ? clientState.signal_strength : 1.0;
        var bars = Math.round(sigStrength * 4);
        var startX = 100;
        var startY = 128;
        for (var i = 0; i < 4; i++) {
            var barHeight = (i + 1) * 3;
            ctx.fillStyle = (i < bars) ? text_color : "rgba(0,0,0,0.15)";
            ctx.fillRect(startX + i * 6, startY - barHeight, 4, barHeight);
        }
    }
    
    if (ctxInfo.action !== "com.tacnet.radio.lcd") {
        drawHUDScanlines(ctx, 144, 144);
    }
    setImage(context, canvas.toDataURL());
}

function setImage(context, imgBase64) {
    if (websocket) {
        var json = {
            "event": "setImage",
            "context": context,
            "payload": {
                "image": imgBase64,
                "target": 0
            }
        };
        websocket.send(JSON.stringify(json));
    }
}

function handleKeyEvent(event, action, context, payload) {
    var settings = payload.settings || {};
    
    if (event === "keyDown") {
        if (action === "com.tacnet.radio.ptt") {
            postPTT("on");
        } else if (action === "com.tacnet.radio.channel") {
            var targetCh = (settings.channel !== undefined) ? settings.channel : 1;
            postChannel(targetCh);
        } else if (action === "com.tacnet.radio.vox") {
            var nextVox = clientState ? !clientState.vox : true;
            postVox(nextVox);
        } else if (action === "com.tacnet.radio.volume") {
            var nextAction = (clientState && clientState.volume > 0) ? "mute" : "unmute";
            postVolumeAction(nextAction);
        } else if (action === "com.tacnet.radio.volup") {
            var currentVol = clientState ? clientState.volume : 1.0;
            var nextVol = Math.min(2.0, Math.round((currentVol + 0.1) * 10) / 10);
            postVolume(nextVol);
        } else if (action === "com.tacnet.radio.voldown") {
            var currentVol = clientState ? clientState.volume : 1.0;
            var nextVol = Math.max(0.0, Math.round((currentVol - 0.1) * 10) / 10);
            postVolume(nextVol);
        } else if (action === "com.tacnet.radio.roster") {
            postTab(2);
        } else if (action === "com.tacnet.radio.map") {
            postTab(3);
        } else if (action === "com.tacnet.radio.comsec") {
            postComsec();
        } else if (action === "com.tacnet.radio.member") {
            var targetCs = (settings.callsign || "").trim().toUpperCase();
            if (targetCs) {
                postHighlight(targetCs);
            }
        }
    } else if (event === "keyUp") {
        if (action === "com.tacnet.radio.ptt") {
            postPTT("off");
        }
    }
}

function handleDialEvent(event, action, context, payload) {
    var ticks = payload.ticks || 0;
    var pressed = payload.pressed;
    
    if (event === "dialRotate") {
        if (action === "com.tacnet.radio.volume") {
            var delta = ticks * 0.05;
            var currentVol = clientState ? clientState.volume : 0.8;
            var nextVol = Math.max(0.0, Math.min(2.0, currentVol + delta));
            postVolume(nextVol);
        } else if (action === "com.tacnet.radio.channel") {
            var nextAction = (ticks > 0) ? "next" : "prev";
            postChannelAction(nextAction);
        }
    }
    
    if (event === "dialPress" && pressed) {
        if (action === "com.tacnet.radio.volume") {
            var nextAction = (clientState && clientState.volume > 0) ? "mute" : "unmute";
            postVolumeAction(nextAction);
        } else if (action === "com.tacnet.radio.vox") {
            var nextVox = clientState ? !clientState.vox : true;
            postVox(nextVox);
        }
    }
}

function postPTT(state) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/ptt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "state": state })
    }).then(fetchClientState).catch(err => console.error("PTT POST failed", err));
}

function postChannel(channel) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/channel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "channel": channel })
    }).then(fetchClientState).catch(err => console.error("Channel POST failed", err));
}

function postChannelAction(action) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/channel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "action": action })
    }).then(fetchClientState).catch(err => console.error("Channel Action POST failed", err));
}

function postVolume(level) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/volume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "level": level })
    }).then(fetchClientState).catch(err => console.error("Volume POST failed", err));
}

function postVolumeAction(action) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/volume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "action": action })
    }).then(fetchClientState).catch(err => console.error("Volume Action POST failed", err));
}

function postVox(state) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/vox", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "state": state })
    }).then(fetchClientState).catch(err => console.error("VOX POST failed", err));
}

function postTab(tabIndex) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/tab", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "tab": tabIndex })
    }).then(fetchClientState).catch(err => console.error("Tab POST failed", err));
}

function postComsec() {
    return fetch("http://127.0.0.1:" + apiPort + "/api/comsec", {
        method: "POST",
        headers: { "Content-Type": "application/json" }
    }).then(fetchClientState).catch(err => console.error("Comsec POST failed", err));
}

function postHighlight(callsign) {
    return fetch("http://127.0.0.1:" + apiPort + "/api/member/highlight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "callsign": callsign })
    }).then(fetchClientState).catch(err => console.error("Highlight POST failed", err));
}

function triggerAutoProfileSwitch() {
    var now = Date.now();
    if (now - lastProfileSwitchTime < 10000) { // 10 seconds cooldown
        return;
    }
    
    var deviceId = null;
    for (var context in activeContexts) {
        deviceId = activeContexts[context].device;
        if (deviceId) break;
    }
    if (!deviceId) return;
    
    var profileName = "TacNet Grid";
    var deviceInfo = devices[deviceId];
    if (deviceInfo) {
        if (deviceInfo.type === 2) { // XL
            profileName = "TacNet Grid XL";
        } else if (deviceInfo.type === 7) { // Plus
            profileName = "TacNet Plus";
        }
    }
    
    if (websocket) {
        var json = {
            "event": "switchToProfile",
            "context": pluginUUID,
            "device": deviceId,
            "payload": {
                "profile": profileName
            }
        };
        websocket.send(JSON.stringify(json));
        lastProfileSwitchTime = now;
    }
}

function updateEncoderFeedback(context, ctxInfo) {
    if (!websocket || !clientState) return;
    
    var payload = {};
    if (ctxInfo.action === "com.tacnet.radio.volume") {
        var volPercent = Math.round(clientState.volume * 100);
        var isMute = (volPercent === 0);
        payload = {
            "title": isMute ? "Muted" : "Volume",
            "value": volPercent + "%",
            "indicator": Math.min(100, Math.max(0, volPercent))
        };
    } else if (ctxInfo.action === "com.tacnet.radio.channel") {
        var targetCh = (ctxInfo.settings.channel !== undefined) ? ctxInfo.settings.channel : 1;
        var isActive = (clientState.current_channel === targetCh);
        var chName = (clientState.channels && clientState.channels[targetCh]) ? clientState.channels[targetCh] : ("PRESET " + targetCh);
        payload = {
            "title": isActive ? "Active Channel" : "Preset Channel",
            "value": "CH " + targetCh,
            "subtitle": chName
        };
    }
    
    if (Object.keys(payload).length > 0) {
        var json = {
            "event": "setFeedback",
            "context": context,
            "payload": payload
        };
        websocket.send(JSON.stringify(json));
    }
}

function drawGlowingWave(ctx, yCenter, level, color) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.shadowBlur = 8;
    ctx.shadowColor = color;
    ctx.beginPath();
    var phase = animationFrame * 0.25;
    var amplitude = Math.min(45, (level || 0.05) * 35);
    if (amplitude < 4) amplitude = 4;

    for (var x = 10; x <= 134; x++) {
        var angle = (x / 18) + phase;
        var envelope = Math.sin((x - 10) / 124 * Math.PI);
        var y = yCenter + Math.sin(angle) * amplitude * envelope;
        if (x === 10) {
            ctx.moveTo(x, y);
        } else {
            ctx.lineTo(x, y);
        }
    }
    ctx.stroke();
    ctx.restore();
}

// Animation loop at ~6.6 FPS (150ms) for real-time waveform visualizers and unselected channel traffic alerts
setInterval(function() {
    animationFrame++;
    if (!clientState) return;
    for (var context in activeContexts) {
        var ctxInfo = activeContexts[context];
        if (!ctxInfo) continue;
        
        var shouldAnimate = false;
        if (ctxInfo.action === "com.tacnet.radio.ptt" && clientState.tx_active) {
            shouldAnimate = true;
        } else if (ctxInfo.action === "com.tacnet.radio.status" && clientState.rx_active) {
            shouldAnimate = true;
        } else if (ctxInfo.action === "com.tacnet.radio.channel") {
            var targetCh = (ctxInfo.settings.channel !== undefined) ? ctxInfo.settings.channel : 1;
            var hasActivity = clientState.active_channels && clientState.active_channels.includes(targetCh);
            var isActive = (clientState.current_channel === targetCh);
            if (hasActivity && !isActive) {
                shouldAnimate = true;
            }
        }
        
        if (shouldAnimate) {
            updateContextState(context);
        }
    }
}, 150);

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
        colorCenter = isActive ? "#3b0764" : "#1e293b";
        colorEdge = isActive ? "#120024" : "#0f172a";
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

// Clear visual shadow offsets
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
