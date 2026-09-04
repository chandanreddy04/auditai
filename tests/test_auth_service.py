from app.services import auth_service


def test_hash_password_returns_hash_and_salt():
    password_hash, salt = auth_service.hash_password("correct-horse-battery-staple")
    assert password_hash
    assert salt
    assert password_hash != "correct-horse-battery-staple"


def test_verify_password_succeeds_with_correct_password():
    password_hash, salt = auth_service.hash_password("correct-horse-battery-staple")
    assert auth_service.verify_password("correct-horse-battery-staple", password_hash, salt) is True


def test_verify_password_fails_with_wrong_password():
    password_hash, salt = auth_service.hash_password("correct-horse-battery-staple")
    assert auth_service.verify_password("wrong-password", password_hash, salt) is False


def test_same_password_produces_different_hash_each_time_due_to_random_salt():
    hash1, salt1 = auth_service.hash_password("same-password")
    hash2, salt2 = auth_service.hash_password("same-password")
    assert salt1 != salt2
    assert hash1 != hash2
    # but both still verify correctly against their own salt
    assert auth_service.verify_password("same-password", hash1, salt1) is True
    assert auth_service.verify_password("same-password", hash2, salt2) is True
