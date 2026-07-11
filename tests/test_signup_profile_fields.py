import os
import unittest
from io import BytesIO

from app import app, db, User


class SignupProfileFieldsTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.app_context = app.app_context()
        self.app_context.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_signup_saves_profile_picture_and_interest(self):
        client = app.test_client()

        response = client.post(
            "/signup",
            data={
                "full_name": "Ada Lovelace",
                "email": "ada@example.com",
                "password": "password123",
                "confirm_password": "password123",
                "interest": "Gaming, Travel",
                "profile_picture": (BytesIO(b"fake-image-data"), "avatar.png"),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        user = User.query.filter_by(email="ada@example.com").first()
        self.assertIsNotNone(user)
        self.assertTrue(user.profile_picture and user.profile_picture.startswith("pic/"))
        self.assertEqual(user.interests, "Gaming, Travel")
        self.assertTrue(os.path.exists(os.path.join(app.root_path, user.profile_picture)))


if __name__ == "__main__":
    unittest.main()
