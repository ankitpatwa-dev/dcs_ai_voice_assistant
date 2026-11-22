import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

export class AiMicrophone extends Component {
    static template = "dcs_ai_voice_assistant.Microphone";
    static props = { size: { type: String, optional: true } };

    setup() {
        this.ai = useService("dcs_ai_assistant");
        this.state = useState(this.ai.state);
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
