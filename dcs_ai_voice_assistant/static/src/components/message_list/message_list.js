import { Component, markup, onMounted, onPatched, proxy, signal, t, usePlugin, useProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { AiAssistantPlugin } from "../../services/ai_assistant_plugin";

function escapeHtml(text) {
    return String(text ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/** Minimal, safe markdown: everything is escaped first, then a few inline patterns are applied. */
export function renderText(text) {
    const lines = escapeHtml(text).split(/\r?\n/);
    const out = [];
    let inList = false;
    for (const raw of lines) {
        let line = raw
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            .replace(/`([^`]+)`/g, "<code>$1</code>")
            .replace(/(^|[\s(])\*(?!\s)([^*]+?)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
        const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
        if (item) {
            if (!inList) {
                out.push("<ul>");
                inList = true;
            }
            out.push(`<li>${item[1]}</li>`);
            continue;
        }
        if (inList) {
            out.push("</ul>");
            inList = false;
        }
        line = line.replace(/^#{1,4}\s+(.*)$/, "<strong>$1</strong>");
        out.push(line ? `<p>${line}</p>` : "");
    }
    if (inList) {
        out.push("</ul>");
    }
    return markup(out.join(""));
}

export class AiDisplayBlock extends Component {
    static template = "dcs_ai_voice_assistant.DisplayBlock";
    props = useProps({ block: t.object() });
    ai = usePlugin(AiAssistantPlugin);
    local = proxy({ expanded: false });

    get visibleRows() {
        const rows = this.props.block.rows || [];
        return this.local.expanded ? rows : rows.slice(0, 8);
    }

    rowsOf(table) {
        const rows = table.rows || [];
        return this.local.expanded ? rows : rows.slice(0, 8);
    }

    toggleExpanded() {
        this.local.expanded = !this.local.expanded;
    }

    open(row, model) {
        const target = row.link || (row.id && { model: model || this.props.block.model, id: row.id });
        if (target?.model && target?.id) {
            this.ai.openRecord(target.model, target.id);
        }
    }

    isClickable(row, model) {
        return Boolean(row.link || (row.id && (model || this.props.block.model)));
    }

    isNumeric(col) {
        return ["integer", "float", "monetary"].includes(col.type);
    }
}

export class AiActionCard extends Component {
    static template = "dcs_ai_voice_assistant.ActionCard";
    props = useProps({ action: t.object(), compact: t.boolean().optional() });
    ai = usePlugin(AiAssistantPlugin);

    get stateLabel() {
        return {
            pending: _t("Awaiting your confirmation"),
            done: _t("Done"),
            failed: _t("Failed"),
            cancelled: _t("Cancelled"),
            expired: _t("Expired"),
        }[this.props.action.state];
    }

    get stateClass() {
        return {
            pending: "text-bg-warning",
            done: "text-bg-success",
            failed: "text-bg-danger",
            cancelled: "text-bg-secondary",
            expired: "text-bg-secondary",
        }[this.props.action.state];
    }

    confirm() {
        this.ai.decide([this.props.action.id], true);
    }

    cancel() {
        this.ai.decide([this.props.action.id], false);
    }

    openRecord() {
        this.ai.openRecord(this.props.action.model, this.props.action.res_id);
    }
}

export class AiMessageList extends Component {
    static template = "dcs_ai_voice_assistant.MessageList";
    static components = { AiDisplayBlock, AiActionCard };
    props = useProps({ suggestions: t.array().optional() });
    ai = usePlugin(AiAssistantPlugin);
    root = signal.ref();

    setup() {
        this.lastCount = 0;
        this.renderText = renderText;
        onMounted(() => this.scrollToEnd());
        onPatched(() => this.scrollIfNew());
    }

    get state() {
        return this.ai.state;
    }

    scrollIfNew() {
        const count = this.state.messages.length + (this.state.busy ? 1 : 0);
        if (count !== this.lastCount) {
            this.lastCount = count;
            this.scrollToEnd();
        }
    }

    scrollToEnd() {
        // Child cards render after this patch: scroll once layout has settled.
        const el = this.root();
        if (!el) {
            return;
        }
        el.scrollTop = el.scrollHeight;
        requestAnimationFrame(() => (el.scrollTop = el.scrollHeight));
        setTimeout(() => (el.scrollTop = el.scrollHeight), 120);
    }

    useSuggestion(text) {
        this.ai.send(text);
    }

    isSpeaking(message) {
        return this.state.speakingMessageId === message.id;
    }

    toggleSpeak(message) {
        if (this.isSpeaking(message)) {
            this.ai.stopSpeaking();
        } else {
            this.ai.speak(message);
        }
    }

    get phaseLabel() {
        return {
            thinking: _t("Working on it…"),
            transcribing: _t("Transcribing…"),
        }[this.state.phase] || _t("Working on it…");
    }
}
