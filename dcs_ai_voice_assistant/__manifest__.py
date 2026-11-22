# Copyright 2026 Devscodespace
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "AI Voice Assistant",
    "version": "18.0.1.0.0",
    "summary": "Talk to Odoo: ask questions about your data, create and update records "
               "by voice or text, with confirmations, access rights and an audit log.",
    "description": """
AI Voice Assistant for Odoo 18 Community
========================================
A voice and chat assistant that works on real Odoo records through a fixed set of
validated tools (no AI-generated Python or SQL). Every operation runs as the
signed-in user, so access rights, record rules and multi-company restrictions apply.
Changes are prepared first and only executed after the user confirms them.
""",
    "category": "Productivity/Artificial Intelligence",
    "author": "Devscodespace",
    "website": "https://devscodespace.com",
    "license": "LGPL-3",
    "depends": ["base", "base_setup", "web", "mail"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ai_assistant_data.xml",
        "views/ai_conversation_views.xml",
        "views/ai_action_views.xml",
        "views/ai_settings_views.xml",
        "views/ai_menu_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "dcs_ai_voice_assistant/static/src/scss/*.scss",
            "dcs_ai_voice_assistant/static/src/services/*.js",
            "dcs_ai_voice_assistant/static/src/components/**/*.js",
            "dcs_ai_voice_assistant/static/src/xml/*.xml",
        ],
    },
    "images": ["static/description/icon.png"],
    "installable": True,
    "application": True,
}
