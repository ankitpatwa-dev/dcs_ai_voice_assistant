import { Component, onMounted, onWillStart, onWillUnmount, usePlugin, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { AiAssistantPlugin } from "../../services/ai_assistant_plugin";
import { AiChatWindow } from "../chat_window/chat_window";

/** Floating launcher + chat window, mounted once in the web client (available on every screen). */
export class AiFloatingAssistant extends Component {
    static template = "dcs_ai_voice_assistant.FloatingAssistant";
    static components = { AiChatWindow };
    ai = usePlugin(AiAssistantPlugin);

    get state() {
        return this.ai.state;
    }

    get showLauncher() {
        return this.state.ready && this.state.config.show_floating_button && !this.state.open && !this.state.pageOpen;
    }

    open() {
        this.ai.toggle(true);
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
    static components = { AiChatWindow };
    props = useProps(standardActionServiceProps);
    ai = usePlugin(AiAssistantPlugin);

    setup() {
        onWillStart(async () => {
            this.ai.toggle(false);
            await this.ai.init().catch(() => {});
        });
        onMounted(() => (this.ai.state.pageOpen = true));
        onWillUnmount(() => (this.ai.state.pageOpen = false));
    }
}

registry.category("actions").add("dcs_ai_voice_assistant.assistant", AiAssistantPage);
