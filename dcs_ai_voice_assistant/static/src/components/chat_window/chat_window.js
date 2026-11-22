import { Component, onMounted, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { AiMessageList } from "../message_list/message_list";
import { AiMicrophone } from "../microphone/microphone";

export class AiChatWindow extends Component {
    static template = "dcs_ai_voice_assistant.ChatWindow";
    static components = { AiMessageList, AiMicrophone };
    static props = {
        mode: { type: String, optional: true }, // "floating" | "page"
        onClose: { type: Function, optional: true },
    };
    static defaultProps = { mode: "floating" };

    setup() {
        this.ai = useService("dcs_ai_assistant");
        this.state = useState(this.ai.state);
        this.input = useRef("input");
        onMounted(() => this.input.el?.focus());
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
        }[this.state.phase] || "";
    }

    get placeholder() {
        if (!this.state.config.text_enabled) {
            return _t("Text chat is disabled — use the microphone");
        }
        return this.props.mode === "page"
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
        if (this.input.el) {
            this.input.el.value = "";
            this.autosize(this.input.el);
        }
        this.ai.send(text, "text");
    }

    clear() {
        this.ai.clear();
    }

    toggleSpeaker() {
        this.state.speakerOn = !this.state.speakerOn;
        if (!this.state.speakerOn) {
            this.ai.stopSpeaking();
        }
    }
}
