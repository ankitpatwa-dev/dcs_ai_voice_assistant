import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { AiChatWindow } from "../chat_window/chat_window";
import { AiMicrophone } from "../microphone/microphone";

/** Floating launcher + chat window, mounted once in the web client (available on every screen). */
export class AiFloatingAssistant extends Component {
    static template = "dcs_ai_voice_assistant.FloatingAssistant";
    static components = { AiChatWindow };
    static props = {};

    setup() {
        this.ai = useService("dcs_ai_assistant");
        this.state = useState(this.ai.state);
    }

    get showLauncher() {
        return this.state.ready && this.state.config.show_floating_button && !this.state.open && !this.state.pageOpen;
    }

    close() {
        this.ai.toggle(false);
    }
}

registry.category("main_components").add("dcs_ai_voice_assistant.FloatingAssistant", {
    Component: AiFloatingAssistant,
    props: {},
});

/** Full-screen assistant (menu AI Assistant > Assistant). */
export class AiAssistantPage extends Component {
    static template = "dcs_ai_voice_assistant.AssistantPage";
    static components = { AiChatWindow, AiMicrophone };
    static props = ["*"];

    setup() {
        this.ai = useService("dcs_ai_assistant");
        this.state = useState(this.ai.state);
        onWillStart(async () => {
            this.ai.toggle(false);
            await this.ai.init().catch(() => {});
        });
        onMounted(() => (this.state.pageOpen = true));
        onWillUnmount(() => (this.state.pageOpen = false));
    }
}

registry.category("actions").add("dcs_ai_voice_assistant.assistant", AiAssistantPage);
