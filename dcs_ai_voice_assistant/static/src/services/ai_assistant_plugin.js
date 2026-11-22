import { Plugin, proxy, usePlugin } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { NotificationPlugin } from "@web/core/notifications/notification_plugin";
import { services } from "@web/core/services";
import { user } from "@web/core/user";
import { ActionPlugin } from "@web/webclient/actions/action_plugin";

const MAX_RECORDING_MS = 60000;
const SILENCE_MS = 1800;

function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(String(reader.result).split(",")[1] || "");
        reader.onerror = reject;
        reader.readAsDataURL(blob);
    });
}

/** Text suitable for reading aloud: no markdown, no very long lists. */
export function speakableText(text) {
    return (text || "")
        .replace(/```[\s\S]*?```/g, "")
        .replace(/\*\*|__|`|#+\s/g, "")
        .replace(/^\s*[-*]\s+/gm, "")
        .replace(/\|/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 1200);
}

function speechLang(config, userInfo) {
    const lang = config.language && config.language !== "auto" ? config.language : userInfo.lang || "en_US";
    const map = { en: "en-US", hi: "hi-IN", ja: "ja-JP", fr: "fr-FR", de: "de-DE", es: "es-ES", ar: "ar-SA",
        it: "it-IT", pt: "pt-PT", nl: "nl-NL", zh: "zh-CN", gu: "gu-IN", mr: "mr-IN", ta: "ta-IN", bn: "bn-IN" };
    return lang.includes("_") ? lang.replace("_", "-") : map[lang] || lang;
}

/**
 * Global assistant plugin (OWL 3): conversation state, voice input/output and server calls,
 * shared by the floating window, the systray button and the full-page assistant.
 */
export class AiAssistantPlugin extends Plugin {
    setup() {
        const action = usePlugin(ActionPlugin);
        const notification = usePlugin(NotificationPlugin);
        const state = proxy({
            ready: false,
            open: false,
            pageOpen: false,
            config: {},
            user: {},
            conversationId: false,
            messages: [],
            busy: false,
            phase: "idle", // idle | listening | transcribing | thinking | speaking
            interim: "",
            draft: "",
            speakerOn: false,
            speakingMessageId: null,
            audioPaused: false,
            level: 0,
        });
        let initPromise = null;
        let recorder = null;
        let recognition = null;
        let audio = null;
        let audioCtx = null;
        let stopTimers = [];

        const ctx = () => user.context;

        async function init(force = false) {
            if (initPromise && !force) {
                return initPromise;
            }
            initPromise = rpc("/dcs_ai/init", { context: ctx() }, { silent: true })
                .then((res) => {
                    state.config = res.config;
                    state.user = res.user;
                    state.conversationId = res.conversation_id;
                    state.messages = res.messages;
                    state.ready = true;
                })
                .catch((e) => {
                    initPromise = null;
                    throw e;
                });
            return initPromise;
        }

        function pushMessages(messages) {
            for (const m of messages || []) {
                if (!state.messages.find((x) => x.id === m.id)) {
                    state.messages.push(m);
                }
            }
        }

        function applyActionUpdates(updated) {
            if (!updated?.length) {
                return;
            }
            const byId = Object.fromEntries(updated.map((a) => [a.id, a]));
            for (const msg of state.messages) {
                for (let i = 0; i < (msg.actions || []).length; i++) {
                    if (byId[msg.actions[i].id]) {
                        msg.actions[i] = byId[msg.actions[i].id];
                    }
                }
            }
        }

        function errorText(e) {
            return e?.data?.message || e?.message || _t("Something went wrong.");
        }

        function localError(text) {
            state.messages.push({ id: `local-${Date.now()}`, role: "assistant", content: text, is_error: true,
                display: [], actions: [] });
        }

        async function send(text, inputMode = "text") {
            text = (text || "").trim();
            if (!text || state.busy) {
                return;
            }
            await init();
            stopSpeaking();
            const tempId = `tmp-${Date.now()}`;
            state.messages.push({ id: tempId, role: "user", content: text, input_mode: inputMode, display: [],
                actions: [], pending: true });
            state.busy = true;
            state.phase = "thinking";
            try {
                const res = await rpc("/dcs_ai/chat", {
                    message: text,
                    conversation_id: state.conversationId,
                    input_mode: inputMode,
                    context: ctx(),
                }, { silent: true });
                state.messages = state.messages.filter((m) => m.id !== tempId);
                state.conversationId = res.conversation_id;
                pushMessages(res.messages);
                applyActionUpdates(res.updated_actions);
                const last = res.messages[res.messages.length - 1];
                if (last?.role === "assistant" && (state.speakerOn || (inputMode === "voice" && state.config.auto_speak))) {
                    speak(last);
                }
            } catch (e) {
                const tmp = state.messages.find((m) => m.id === tempId);
                if (tmp) {
                    tmp.pending = false;
                }
                localError(errorText(e));
            } finally {
                state.busy = false;
                if (state.phase === "thinking") {
                    state.phase = "idle";
                }
            }
        }

        async function decide(actionIds, approve) {
            if (state.busy) {
                return;
            }
            state.busy = true;
            state.phase = "thinking";
            try {
                const res = await rpc("/dcs_ai/action/decide", { action_ids: actionIds, approve, context: ctx() },
                    { silent: true });
                applyActionUpdates(res.updated_actions);
                pushMessages(res.messages);
                const last = res.messages[res.messages.length - 1];
                if (last && state.speakerOn) {
                    speak(last);
                }
            } catch (e) {
                localError(errorText(e));
            } finally {
                state.busy = false;
                state.phase = "idle";
            }
        }

        async function clear() {
            stopSpeaking();
            cancelListening();
            const id = state.conversationId;
            state.messages = [];
            state.conversationId = false;
            if (id) {
                try {
                    await rpc("/dcs_ai/conversation/clear", { conversation_id: id, context: ctx() }, { silent: true });
                } catch (e) {
                    notification.add(errorText(e), { type: "danger" });
                }
            }
        }

        // ------------------------------------------------------------ voice input
        function browserRecognitionClass() {
            return window.SpeechRecognition || window.webkitSpeechRecognition;
        }

        function voiceSupported() {
            if (!state.config.voice_enabled) {
                return false;
            }
            if (state.config.stt_provider === "browser") {
                return Boolean(browserRecognitionClass());
            }
            return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
        }

        async function toggleListening() {
            if (state.phase === "listening") {
                return stopListening();
            }
            if (state.busy) {
                return;
            }
            await init();
            stopSpeaking();
            if (!voiceSupported()) {
                notification.add(
                    state.config.voice_enabled
                        ? _t("Voice input is not supported by this browser. Try Chrome or Edge, or type your request.")
                        : _t("Voice input is disabled by the administrator."),
                    { type: "warning" }
                );
                return;
            }
            if (state.config.stt_provider === "browser") {
                return startBrowserRecognition();
            }
            return startRecording();
        }

        function startBrowserRecognition() {
            const Recognition = browserRecognitionClass();
            recognition = new Recognition();
            recognition.lang = speechLang(state.config, state.user);
            recognition.interimResults = true;
            recognition.continuous = false;
            let finalText = "";
            recognition.onresult = (ev) => {
                let interim = "";
                for (let i = ev.resultIndex; i < ev.results.length; i++) {
                    const r = ev.results[i];
                    if (r.isFinal) {
                        finalText += r[0].transcript;
                    } else {
                        interim += r[0].transcript;
                    }
                }
                state.interim = (finalText + " " + interim).trim();
            };
            recognition.onerror = (ev) => {
                if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
                    notification.add(_t("Microphone access was refused. Allow it in the browser to use voice."),
                        { type: "warning" });
                } else if (ev.error !== "no-speech" && ev.error !== "aborted") {
                    notification.add(_t("Speech recognition error: %s", ev.error), { type: "warning" });
                }
            };
            recognition.onend = () => {
                const text = (finalText || state.interim).trim();
                recognition = null;
                state.interim = "";
                if (state.phase === "listening") {
                    state.phase = "idle";
                    if (text) {
                        send(text, "voice");
                    }
                }
            };
            state.phase = "listening";
            state.interim = "";
            recognition.start();
        }

        async function startRecording() {
            let stream;
            try {
                stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            } catch {
                notification.add(_t("Microphone access was refused or no microphone was found."), { type: "warning" });
                return;
            }
            const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find(
                (t) => window.MediaRecorder.isTypeSupported?.(t)
            );
            recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
            const chunks = [];
            recorder.ondataavailable = (ev) => ev.data.size && chunks.push(ev.data);
            recorder.onstop = async () => {
                stream.getTracks().forEach((t) => t.stop());
                closeAnalyser();
                const cancelled = recorder?._cancelled;
                const type = recorder?.mimeType || "audio/webm";
                recorder = null;
                if (cancelled || !chunks.length) {
                    state.phase = "idle";
                    return;
                }
                state.phase = "transcribing";
                try {
                    const blob = new Blob(chunks, { type });
                    const res = await rpc("/dcs_ai/transcribe", {
                        audio: await blobToBase64(blob), mimetype: type, context: ctx(),
                    }, { silent: true });
                    state.phase = "idle";
                    if (res.error) {
                        notification.add(res.error, { type: "danger" });
                    } else if (res.text) {
                        await send(res.text, "voice");
                    } else {
                        notification.add(_t("I didn't catch that. Please try again."), { type: "info" });
                    }
                } catch (e) {
                    state.phase = "idle";
                    notification.add(errorText(e), { type: "danger" });
                }
            };
            recorder.start(250);
            state.phase = "listening";
            watchSilence(stream);
            stopTimers.push(setTimeout(stopListening, MAX_RECORDING_MS));
        }

        function watchSilence(stream) {
            try {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                const source = audioCtx.createMediaStreamSource(stream);
                const analyser = audioCtx.createAnalyser();
                analyser.fftSize = 512;
                source.connect(analyser);
                const data = new Uint8Array(analyser.fftSize);
                let heard = false;
                let silentSince = Date.now();
                const tick = () => {
                    if (!audioCtx || state.phase !== "listening") {
                        return;
                    }
                    analyser.getByteTimeDomainData(data);
                    let sum = 0;
                    for (const v of data) {
                        sum += (v - 128) * (v - 128);
                    }
                    const rms = Math.sqrt(sum / data.length) / 128;
                    state.level = Math.min(1, rms * 6);
                    if (rms > 0.04) {
                        heard = true;
                        silentSince = Date.now();
                    } else if (heard && Date.now() - silentSince > SILENCE_MS) {
                        stopListening();
                        return;
                    }
                    requestAnimationFrame(tick);
                };
                requestAnimationFrame(tick);
            } catch {
                // Level meter and auto-stop are optional.
            }
        }

        function closeAnalyser() {
            state.level = 0;
            if (audioCtx) {
                audioCtx.close().catch(() => {});
                audioCtx = null;
            }
            stopTimers.forEach(clearTimeout);
            stopTimers = [];
        }

        function stopListening() {
            if (recognition) {
                recognition.stop();
            } else if (recorder && recorder.state !== "inactive") {
                recorder.stop();
            }
        }

        function cancelListening() {
            if (recognition) {
                state.phase = "idle";
                recognition.abort();
            } else if (recorder && recorder.state !== "inactive") {
                recorder._cancelled = true;
                recorder.stop();
            }
            state.interim = "";
        }

        // ----------------------------------------------------------- voice output
        async function speak(message) {
            stopSpeaking();
            const text = speakableText(message.content);
            if (!text || !state.config.voice_enabled) {
                return;
            }
            state.speakingMessageId = message.id;
            state.phase = "speaking";
            if (state.config.tts_provider === "browser") {
                if (!window.speechSynthesis) {
                    return stopSpeaking();
                }
                const utter = new SpeechSynthesisUtterance(text);
                utter.lang = speechLang(state.config, state.user);
                const voice = window.speechSynthesis.getVoices().find((v) => v.lang === utter.lang)
                    || window.speechSynthesis.getVoices().find((v) => v.lang?.startsWith(utter.lang.split("-")[0]));
                if (voice) {
                    utter.voice = voice;
                }
                utter.onend = utter.onerror = () => {
                    if (state.speakingMessageId === message.id) {
                        resetSpeaking();
                    }
                };
                window.speechSynthesis.speak(utter);
                return;
            }
            try {
                const res = await rpc("/dcs_ai/speak", { text, context: ctx() }, { silent: true });
                if (state.speakingMessageId !== message.id) {
                    return;
                }
                if (res.error) {
                    notification.add(res.error, { type: "warning" });
                    return resetSpeaking();
                }
                audio = new Audio(`data:${res.mimetype};base64,${res.audio}`);
                audio.onended = audio.onerror = () => resetSpeaking();
                await audio.play();
            } catch (e) {
                resetSpeaking();
                notification.add(_t("Could not play the answer: %s", errorText(e)), { type: "warning" });
            }
        }

        function resetSpeaking() {
            state.speakingMessageId = null;
            state.audioPaused = false;
            if (state.phase === "speaking") {
                state.phase = "idle";
            }
        }

        function stopSpeaking() {
            if (window.speechSynthesis?.speaking) {
                window.speechSynthesis.cancel();
            }
            if (audio) {
                audio.pause();
                audio = null;
            }
            resetSpeaking();
        }

        function togglePause() {
            if (state.config.tts_provider === "browser" && window.speechSynthesis) {
                if (window.speechSynthesis.paused) {
                    window.speechSynthesis.resume();
                    state.audioPaused = false;
                } else {
                    window.speechSynthesis.pause();
                    state.audioPaused = true;
                }
            } else if (audio) {
                if (audio.paused) {
                    audio.play();
                    state.audioPaused = false;
                } else {
                    audio.pause();
                    state.audioPaused = true;
                }
            }
        }

        // ---------------------------------------------------------------- misc
        function openRecord(model, resId) {
            if (!model || !resId) {
                return;
            }
            action.doAction({
                type: "ir.actions.act_window",
                res_model: model,
                res_id: resId,
                views: [[false, "form"]],
                target: "current",
            });
        }

        function openFullPage() {
            state.open = false;
            action.doAction("dcs_ai_voice_assistant.action_dcs_ai_assistant");
        }

        function toggle(open = !state.open) {
            if (open && state.pageOpen) {
                return; // the full-page assistant is already on screen
            }
            state.open = open;
            if (open) {
                init().catch((e) => notification.add(errorText(e), { type: "danger" }));
            }
        }

        // Pre-load the configuration (floating button visibility) without blocking the web client.
        init().catch(() => {});

        Object.assign(this, {
            state,
            init,
            send,
            decide,
            clear,
            toggle,
            toggleListening,
            cancelListening,
            speak,
            stopSpeaking,
            togglePause,
            openRecord,
            openFullPage,
            voiceSupported,
        });
    }
}

services.add(AiAssistantPlugin);
