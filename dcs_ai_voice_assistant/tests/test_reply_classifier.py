from odoo.tests import BaseCase, tagged

from ..models.ai_executor import classify_reply


@tagged("post_install", "-at_install", "dcs_ai")
class TestReplyClassifier(BaseCase):

    def test_affirmative(self):
        for text in ("Yes", "yes.", "Yes please", "OK", "okay go ahead", "Sure!", "confirm", "Go ahead",
                     "haan", "Haan ji", "theek hai", "はい", "yes do it"):
            self.assertEqual(classify_reply(text), "yes", text)

    def test_negative(self):
        for text in ("No", "no.", "nope", "Cancel", "cancel it", "don't", "never mind", "nahi", "No thanks"):
            self.assertEqual(classify_reply(text), "no", text)

    def test_changes_go_to_the_model(self):
        for text in ("yes but change the phone to 123", "no, use ABC Corp instead",
                     "yes and also add 5 chairs", "nothing else", "Show me the top 10 customers",
                     "ok wait, make it 50 units", "", "   "):
            self.assertIsNone(classify_reply(text), text)
