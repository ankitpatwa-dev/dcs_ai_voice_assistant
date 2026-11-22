import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class AiSystrayButton extends Component {
    static template = "dcs_ai_voice_assistant.SystrayButton";
    static props = {};

    setup() {
        this.ai = useService("dcs_ai_assistant");
        this.state = useState(this.ai.state);
    }

    onClick() {
        this.ai.toggle();
    }
}

registry.category("systray").add("dcs_ai_voice_assistant.systray", { Component: AiSystrayButton }, { sequence: 35 });
