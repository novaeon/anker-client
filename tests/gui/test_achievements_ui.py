"""Achievement pop-ups, sound, Library summary, achievements window and settings (fake context, offscreen)."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QApplication

from anker_client.core import events as ev
from anker_client.core.models import Achievement
from anker_client.ui import achievement_notifier as notifier_module
from anker_client.ui import achievement_sound
from anker_client.ui.achievement_notifier import MAX_VISIBLE, AchievementNotifier
from anker_client.ui.achievement_sound import AchievementSound
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.achievements import AchievementsDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.pages.library import LibraryPage
from anker_client.ui.pages.settings import SettingsPage
from anker_client.ui.theme.manager import ThemeManager
from anker_client.ui.widgets.achievement_toast import AchievementToast
from anker_client.ui.widgets.achievement_widgets import AchievementRow, format_unlock_time, sort_like_steam

pytestmark = pytest.mark.gui
_KEEP_ALIVE: list[Any] = []


class Nav:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.calls.append((name, args))


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("HOLD_MS", 150), ("RARE_HOLD_MS", 150), ("IN_MS", 20), ("OUT_MS", 20),
                        ("ICON_WAIT_MS", 1500)):
        monkeypatch.setattr(notifier_module, name, value)


@pytest.fixture
def bridge(fake_ctx: Any) -> Any:
    b = QtEventBridge(fake_ctx.events)
    yield b
    b.close()
    _KEEP_ALIVE.append(b)


@pytest.fixture
def loader(fake_ctx: Any) -> ImageLoader:
    image_loader = ImageLoader(fake_ctx.images, fake_ctx.runner)
    _KEEP_ALIVE.append(image_loader)
    return image_loader


@pytest.fixture
def sound() -> AchievementSound:
    return AchievementSound()  # the shipped resources/achievement.wav (tests never play it)


@pytest.fixture
def notifier(qtbot: Any, fake_ctx: Any, bridge: Any, loader: ImageLoader, sound: AchievementSound) -> Any:
    n = AchievementNotifier(fake_ctx, bridge, loader, sound)
    yield n
    n.shutdown()
    _KEEP_ALIVE.append(n)


def unlocked(name: str = "Laid To Dress", percent: float | None = 81.5, icon: str = "", **kw: Any) -> Achievement:
    return Achievement(api_name=name.upper().replace(" ", "_"), name=name, global_percent=percent,
                       icon_url=icon, unlocked=True, **kw)


def event(achievement: Achievement, **kw: Any) -> ev.AchievementUnlocked:
    return ev.AchievementUnlocked(app_id=4019220, achievement=achievement, game_title="Dressmaker",
                                  install_id="dressmaker", **kw)


# --- toast ----------------------------------------------------------------------------------


def test_toast_texts_follow_steam(qtbot: Any) -> None:
    normal = AchievementToast(unlocked(), unlocked_count=3, total=32)
    rare = AchievementToast(unlocked("Trim and Proper", 3.1))
    rarest = AchievementToast(unlocked("Ghost", 0.04))
    for toast in (normal, rare, rarest):
        qtbot.addWidget(toast)
    assert (normal.title_text, normal.detail_text) == ("Achievement Unlocked!", "3 of 32 achievements")
    assert not normal.rare and rare.rare
    assert rare.title_text == "Rare Achievement Unlocked!"
    assert rare.detail_text == "3.1% of players have this achievement"
    assert rarest.detail_text.startswith("<0.1%")
    assert AchievementToast(unlocked(percent=None)).detail_text == ""


# --- notifier --------------------------------------------------------------------------------


def test_popups_follow_the_setting_but_previews_always_show(qtbot: Any, fake_ctx: Any, notifier: Any) -> None:
    fake_ctx.settings.update(achievement_popups=False)
    notifier.notify(event(unlocked()))
    assert notifier.visible_toasts() == [] and notifier.queued() == 0
    notifier.notify(event(unlocked(), preview=True))
    assert len(notifier.visible_toasts()) == 1


def test_bridge_event_shows_a_popup_and_plays_the_sound(qtbot: Any, fake_ctx: Any, notifier: Any,
                                                        played_sounds: list[Path]) -> None:
    fake_ctx.events.publish(event(unlocked()))
    qtbot.waitUntil(lambda: len(notifier.visible_toasts()) == 1, timeout=3000)
    toast = notifier.visible_toasts()[0]
    assert toast.isVisible() and toast.achievement.name == "Laid To Dress"
    assert [p.name for p in played_sounds] == ["achievement.wav"]
    fake_ctx.events.publish(event(unlocked("Trim and Proper", 3.1)))
    qtbot.waitUntil(lambda: len(played_sounds) == 2, timeout=3000)
    assert played_sounds[-1] == played_sounds[0]  # rare achievements use the same sound


def test_sound_can_be_turned_off(qtbot: Any, fake_ctx: Any, notifier: Any, played_sounds: list[Path]) -> None:
    fake_ctx.settings.update(achievement_sound=False)
    notifier.notify(event(unlocked()))
    assert len(notifier.visible_toasts()) == 1 and played_sounds == []


def test_cards_stack_then_queue_then_drain(qtbot: Any, fake_ctx: Any, notifier: Any) -> None:
    for i in range(MAX_VISIBLE + 2):
        notifier.notify(event(unlocked(f"Achievement {i}")))
    shown = notifier.visible_toasts()
    assert len(shown) == MAX_VISIBLE and notifier.queued() == 2
    ys = sorted({t.pos().y() for t in shown})
    assert len(ys) == MAX_VISIBLE  # one slot each, no overlap of cards
    qtbot.waitUntil(lambda: notifier.queued() == 0, timeout=5000)
    qtbot.waitUntil(lambda: not notifier.visible_toasts(), timeout=5000)


@pytest.mark.parametrize("position", ["bottom_right", "bottom_left", "top_right", "top_left"])
def test_slots_grow_away_from_the_chosen_corner(fake_ctx: Any, notifier: Any, position: str) -> None:
    first, second = notifier.slot_geometry(0, position), notifier.slot_geometry(1, position)
    area = notifier._work_area()
    assert first.x() == second.x()
    assert (first.x() < area.center().x()) == position.endswith("left")
    assert (second.y() > first.y()) == position.startswith("top")


def test_popup_waits_briefly_for_its_icon(qtbot: Any, fake_ctx: Any, notifier: Any) -> None:
    icon = "https://cdn.example/achievements/4019220/laid-to-dress.png"
    notifier.notify(event(unlocked(icon=icon)))
    qtbot.waitUntil(lambda: len(notifier.visible_toasts()) == 1, timeout=3000)
    assert notifier.visible_toasts()[0].has_icon  # shown with its icon, not the placeholder


def test_shutdown_closes_everything(qtbot: Any, notifier: Any) -> None:
    for i in range(5):
        notifier.notify(event(unlocked(f"A{i}")))
    notifier.shutdown()
    assert notifier.visible_toasts() == [] and notifier.queued() == 0
    notifier.notify(event(unlocked()))
    assert notifier.visible_toasts() == []


# --- sound ------------------------------------------------------------------------------------


def test_the_shipped_sound_is_plain_pcm(played_sounds: list[Path]) -> None:
    sound = AchievementSound()
    assert sound.available and sound.path == achievement_sound.resource_path("achievement.wav")
    with wave.open(str(sound.path)) as wav:  # plain PCM: winsound plays it as is
        assert wav.getsampwidth() == 2 and wav.getnframes() > 0
    sound.play()
    assert played_sounds == [sound.path]


def test_a_missing_sound_file_is_silent(tmp_path: Path, played_sounds: list[Path]) -> None:
    sound = AchievementSound(tmp_path / "missing.wav")
    assert not sound.available
    sound.play()
    assert played_sounds == []


# --- library + window ----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _theme(qapp: Any) -> None:
    ThemeManager(qapp).apply("midnight")


@pytest.fixture
def library(qtbot: Any, fake_ctx: Any, bridge: Any, loader: ImageLoader) -> Any:
    page = LibraryPage(fake_ctx, bridge, Nav(), loader)
    qtbot.addWidget(page)
    page.resize(1280, 800)
    page.show()
    qtbot.waitUntil(lambda: page._loaded, timeout=3000)
    yield page
    page.shutdown()
    page.close()
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, AchievementsDialog):
            widget.close()


def test_library_panel_shows_achievements_and_updates_live(qtbot: Any, fake_ctx: Any, library: Any) -> None:
    first, second, third = (g.install_id for g in fake_ctx.library.games()[:3])
    library._set_selected(first, scroll=False)
    panel = library._panel
    qtbot.waitUntil(lambda: panel.achievements.count_label.text() == "5 / 9", timeout=3000)
    assert panel.achievements.isVisible() and panel.achievements.view_button.isVisible()
    fake_ctx.achievements.unlock(first, "FRIENDS")
    qtbot.waitUntil(lambda: panel.achievements.count_label.text() == "6 / 9", timeout=3000)
    library._set_selected(second, scroll=False)
    qtbot.waitUntil(lambda: panel.achievements.message.text() == "No achievements unlocked yet.", timeout=3000)
    library._set_selected(third, scroll=False)  # no Steam emulator: the block disappears
    qtbot.waitUntil(lambda: not panel.achievements.isVisible(), timeout=3000)


def test_view_all_opens_one_window_per_game(qtbot: Any, fake_ctx: Any, library: Any) -> None:
    first = fake_ctx.library.games()[0].install_id
    library._set_selected(first, scroll=False)
    qtbot.waitUntil(library._panel.achievements.view_button.isVisible, timeout=3000)
    library._panel.achievements.view_button.click()
    library._panel.achievements.view_button.click()
    dialogs = [w for w in QApplication.topLevelWidgets() if isinstance(w, AchievementsDialog) and w.isVisible()]
    assert len(dialogs) == 1 and dialogs[0].install_id == first


def test_window_lists_like_steam_and_hides_spoilers(qtbot: Any, fake_ctx: Any, bridge: Any,
                                                    loader: ImageLoader) -> None:
    first = fake_ctx.library.games()[0].install_id
    dialog = AchievementsDialog(fake_ctx, bridge, loader, first, "Hollow Knight")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.data is not None and bool(dialog.rows()), timeout=3000)
    names = [r.achievement.api_name for r in dialog.rows()]
    assert names[:5] == ["SPEEDRUN", "SECRET_ROOM", "NO_DAMAGE", "COLLECTOR", "FIRST_STEPS"]  # newest first
    assert names[5:] == ["FRIENDS", "ALL_SKILLS", "PERFECTIONIST"]  # locked: most common first
    assert "TRUE_ENDING" not in names  # locked + hidden: summarised, not listed
    assert dialog.summary_label.text() == "You've unlocked 5/9 (56%)"
    assert "RUNE" in dialog.source_label.text()
    rare = next(r for r in dialog.rows() if r.achievement.api_name == "SPEEDRUN")
    assert rare.rare_badge is not None and rare.percent_label.text() == "3.6% of players"
    dialog.toggle_hidden()
    assert "TRUE_ENDING" in [r.achievement.api_name for r in dialog.rows()]
    fake_ctx.achievements.unlock(first, "ALL_SKILLS")
    qtbot.waitUntil(lambda: dialog.summary_label.text() == "You've unlocked 6/9 (67%)", timeout=3000)


def test_window_for_a_game_without_emulator(qtbot: Any, fake_ctx: Any, bridge: Any, loader: ImageLoader) -> None:
    third = fake_ctx.library.games()[2].install_id
    dialog = AchievementsDialog(fake_ctx, bridge, loader, third)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(dialog.empty.isVisible, timeout=3000)
    assert not dialog.scroll.isVisible()


def test_sorting_and_dates() -> None:
    items = [Achievement(api_name="old", unlocked=True, unlocked_at=10), Achievement(api_name="new", unlocked=True,
             unlocked_at=20), Achievement(api_name="rare", global_percent=1.0),
             Achievement(api_name="common", global_percent=60.0), Achievement(api_name="unknown")]
    assert [a.api_name for a in sort_like_steam(items)] == ["new", "old", "common", "rare", "unknown"]
    text = format_unlock_time(1_791_319_843)
    assert text.startswith("Unlocked ") and " @ " in text and text.endswith(("am", "pm"))
    assert format_unlock_time(0) == "Unlocked"


def test_progress_rows(qtbot: Any, loader: ImageLoader) -> None:
    row = AchievementRow(Achievement(api_name="X", name="Collector", progress=31, max_progress=50), loader)
    qtbot.addWidget(row)
    assert row.progress_bar is not None and row.progress_bar.value() == 31
    done = AchievementRow(Achievement(api_name="Y", name="Done", unlocked=True, progress=50, max_progress=50), loader)
    qtbot.addWidget(done)
    assert done.progress_bar is None


# --- settings ------------------------------------------------------------------------------------


def test_settings_controls_and_preview(qtbot: Any, fake_ctx: Any, bridge: Any) -> None:
    from anker_client.ui.widgets.settings_controls import ToggleSwitch

    page = SettingsPage(fake_ctx, bridge, Nav(), ThemeManager(QApplication.instance()))  # type: ignore[arg-type]
    qtbot.addWidget(page)
    page.show()
    page.ensure_section("general")
    for key in ("achievement_popups", "achievement_sound"):
        toggle = page.findChild(ToggleSwitch, f"setting_{key}")
        assert toggle is not None and toggle.isChecked()
        toggle.click()
        assert getattr(fake_ctx.settings.get(), key) is False
    combo = page.achievement_position_combo
    combo.setCurrentIndex(combo.findData("top_left"))
    combo.activated.emit(combo.currentIndex())
    assert fake_ctx.settings.get().achievement_position == "top_left"
    seen: list[ev.AchievementUnlocked] = []
    fake_ctx.events.subscribe(ev.AchievementUnlocked, seen.append)
    page.preview_rare_button.click()
    assert len(seen) == 1 and seen[0].preview and seen[0].achievement.rare
    page.shutdown()


def test_invalid_position_falls_back(fake_ctx: Any) -> None:
    fake_ctx.settings.update(achievement_position="middle")
    assert fake_ctx.settings.get().achievement_position == "bottom_right"


# --- visuals ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("theme_key", ["midnight", "daylight"])
def test_screenshots(qtbot: Any, qapp: Any, fake_ctx: Any, bridge: Any, loader: ImageLoader, theme_key: str) -> None:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QColor, QPainter, QPixmap

    from tests.fakes import screenshot

    theme = ThemeManager(qapp)
    theme.apply(theme_key)
    try:
        first = fake_ctx.library.games()[0].install_id
        dialog = AchievementsDialog(fake_ctx, bridge, loader, first, "Hollow Knight")
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitUntil(lambda: bool(dialog.rows()), timeout=3000)
        assert screenshot(dialog, f"achievements_window_{theme_key}", (760, 680)).exists()
        if theme_key == "midnight":  # the pop-up looks the same in every theme
            icon = "https://cdn.example/achievements/1000001/{}.png"
            toasts = [AchievementToast(unlocked("Against the Clock", 41.0, icon.format("against-the-clock")), loader,
                                       unlocked_count=6, total=9),
                      AchievementToast(unlocked("The Last Light", 3.6, icon.format("the-last-light")), loader)]
            for toast in toasts:
                qtbot.addWidget(toast)
                qtbot.waitUntil(lambda t=toast: t.has_icon, timeout=3000)
            toasts[1].set_phase(0.45)
            size = toasts[0].size()
            canvas = QPixmap(size.width(), size.height() * 2)
            canvas.fill(QColor("#3b4a5a"))
            painter = QPainter(canvas)
            for index, toast in enumerate(toasts):
                toast.render(painter, QPoint(0, index * size.height()))
            painter.end()
            out = Path(__file__).resolve().parents[2] / "build" / "screens" / "achievement_popups.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            assert canvas.save(str(out))
    finally:
        theme.apply("midnight")
