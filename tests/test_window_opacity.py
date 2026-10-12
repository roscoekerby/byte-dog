"""The main window is solid by default; the View menu toggle makes it see-through."""
from bytedog import SEE_THROUGH_ALPHA


def test_window_is_solid_by_default(dog):
    assert dog.see_through.get() is False
    assert float(dog.root.attributes('-alpha')) == 1.0


def test_see_through_toggle_round_trips(dog):
    try:
        dog.see_through.set(True)
        dog.apply_see_through()
        assert abs(float(dog.root.attributes('-alpha')) - SEE_THROUGH_ALPHA) < 0.01
    finally:
        dog.see_through.set(False)
        dog.apply_see_through()
    assert float(dog.root.attributes('-alpha')) == 1.0
