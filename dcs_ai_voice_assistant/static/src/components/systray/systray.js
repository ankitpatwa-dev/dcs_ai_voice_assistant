import { Component, usePlugin } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { AiAssistantPlugin } from "../../services/ai_assistant_plugin";

export class AiSystrayButton extends Component {
    static template = "dcs_ai_voice_assistant.SystrayButton";
    ai = usePlugin(AiAssistantPlugin);

    get listening() {
        return this.ai.state.phase === "listening";
    }

    onClick() {
        this.ai.toggle();
    }
}

registry.category("systray").add("dcs_ai_voice_assistant.systray", { Component: AiSystrayButton }, { sequence: 35 });
