import { Component, onMounted, signal, t, usePlugin, useProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { AiAssistantPlugin } from "../../services/ai_assistant_plugin";
import { AiMessageList } from "../message_list/message_list";
import { AiMicrophone } from "../microphone/microphone";

export class AiChatWindow extends Component {
    static template = "dcs_ai_voice_assistant.ChatWindow";
    static components = { AiMessageList, AiMicrophone };
    props = useProps({
        mode: t.string().optional("floating"), // "floating" | "page"
        onClose: t.function().optional(),
    });
    ai = usePlugin(AiAssistantPlugin);
    input = signal.ref();

    setup() {
        onMounted(() => this.input()?.focus());
    }

    get state() {
        return this.ai.state;
    }

    get mode() {
        return this.props.mode || "floating";
    }

    get suggestions() {
        const items = [
            _t("Show me the top 10 customers by sales"),
            _t("Which leads need follow-up?"),
            _t("How much money do customers owe us?"),
            _t("Which products are running low on stock?"),
        ];
        if (this.state.config.allow_create) {
            items.push(_t("Create a new customer named ABC Technologies"));
        }
        items.push(_t("Generate this month's financial summary"));
        return items;
    }

    get statusText() {
        return {
            listening: this.state.interim || _t("Listening… speak now"),
            transcribing: _t("Transcribing your voice…"),
            thinking: _t("Thinking…"),
            speaking: _t("Speaking…"),
        }[this.state.phase] || _t("Ask questions, create and update records");
    }

    get placeholder() {
        if (this.state.config.text_enabled === false) {
            return _t("Text chat is disabled — use the microphone");
        }
        return this.mode === "page"
            ? _t("Ask anything about your business, or tell me what to create or update…")
            : _t("Ask or give a command…");
    }

    get canSend() {
        return !this.state.busy && this.state.draft.trim().length > 0 && this.state.config.text_enabled !== false;
    }

    onInput(ev) {
        this.state.draft = ev.target.value;
        this.autosize(ev.target);
    }

    autosize(el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 140) + "px";
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
            ev.preventDefault();
            this.submit();
        } else if (ev.key === "Escape" && this.props.onClose) {
            this.props.onClose();
        }
    }

    submit() {
        if (!this.canSend) {
            return;
        }
        const text = this.state.draft;
        this.state.draft = "";
        const el = this.input();
        if (el) {
            el.value = "";
            this.autosize(el);
        }
        this.ai.send(text, "text");
    }

    clear() {
        this.ai.clear();
    }

    close() {
        this.props.onClose?.();
    }

    toggleSpeaker() {
        this.state.speakerOn = !this.state.speakerOn;
        if (!this.state.speakerOn) {
            this.ai.stopSpeaking();
        }
    }
}
