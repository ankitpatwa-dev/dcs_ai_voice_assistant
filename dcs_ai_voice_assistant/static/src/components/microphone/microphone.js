import { Component, t, usePlugin, useProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { AiAssistantPlugin } from "../../services/ai_assistant_plugin";

export class AiMicrophone extends Component {
    static template = "dcs_ai_voice_assistant.Microphone";
    props = useProps({ size: t.string().optional() });
    ai = usePlugin(AiAssistantPlugin);

    get state() {
        return this.ai.state;
    }

    get listening() {
        return this.state.phase === "listening";
    }

    get title() {
        if (this.listening) {
            return _t("Stop and send");
        }
        if (this.state.phase === "transcribing") {
            return _t("Transcribing…");
        }
        return _t("Speak a command");
    }

    get ringStyle() {
        return `transform: scale(${1 + (this.state.level || 0) * 0.6});`;
    }

    onClick() {
        this.ai.toggleListening();
    }
}
