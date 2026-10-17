"""Offline regression tests: all export files and Telegram data are synthetic."""
import asyncio
import io
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram_digest as digest


UTC = timezone.utc
NOW = datetime(2026, 10, 17, 12, 0, 0, 123456, tzinfo=UTC)


def checkpoint(directory, name, end, complete=True):
    directory.mkdir(exist_ok=True)
    path = directory / name
    text = ("# Telegram academic messages\nRange: synthetic\n"
            f"Export end (UTC): {end}\n\n## Synthetic course\nNo messages in range.\n")
    if complete:
        text += "\n<!-- Export complete -->\n"
    path.write_text(text, encoding="utf-8")
    return path


def dialog(chat_id=-1):
    return SimpleNamespace(
        id=chat_id, name="Synthetic course", is_group=True, is_channel=False,
        entity=SimpleNamespace(chat_id=chat_id, username="synthetic_course"),
    )


def message(mid, date, body=None, media=None):
    return SimpleNamespace(
        id=mid, date=date, raw_text=body if body is not None else f"Synthetic message {mid}",
        media=media, sender=None, reply_to_msg_id=None,
    )


class FakeClient:
    def __init__(self):
        self.dialogs = [dialog()]
        self.messages = {-1: []}
        self.exit_error = None
        self.disconnected = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.disconnected = True
        if self.exit_error:
            raise self.exit_error

    async def iter_dialogs(self):
        for item in self.dialogs:
            yield item

    async def iter_messages(self, entity):
        for item in self.messages[entity.chat_id]:
            if isinstance(item, BaseException):
                raise item
            yield item


class FrozenDateTime(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class TemporaryExports(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.exports = self.root / "exports"
        self.patch("sys.stdout", io.StringIO())

    def patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def legacy(self, name):
        self.exports.mkdir(exist_ok=True)
        path = self.exports / name
        path.write_text("# Telegram academic messages\nRange: synthetic\n\n"
                        "Synthetic historical content.\n", encoding="utf-8")
        return path


class CheckpointTests(TemporaryExports):
    def test_first_run_without_directory_defaults_to_24_hours(self):
        self.assertEqual(digest.export_start(self.exports, None, NOW), NOW - timedelta(hours=24))
        self.assertFalse(self.exports.exists())

    def test_empty_directory_defaults_to_24_hours(self):
        self.exports.mkdir()
        self.assertEqual(digest.export_start(self.exports, None, NOW), NOW - timedelta(days=1))

    def test_latest_end_wins_over_filename_mtime_and_message_time(self):
        older = checkpoint(self.exports, "telegram_2026-10-17_130000_last1d.md",
                           "2026-10-16T12:00:00+00:00")
        latest = checkpoint(self.exports, "telegram_2000-01-01_000000_last14d.md",
                            "2026-10-17T11:00:00.654321Z")
        text = older.read_text(encoding="utf-8").replace(
            "No messages in range.", "**2026-10-17 13:59 CEST**\n\n> Synthetic content")
        older.write_text(text, encoding="utf-8")
        os.utime(older, (2000000000, 2000000000))
        os.utime(latest, (1, 1))
        self.assertEqual(digest.export_start(self.exports, None, NOW),
                         datetime(2026, 10, 17, 11, 0, 0, 654321, tzinfo=UTC))

    def test_legacy_filename_uses_rome_summer_and_winter_offsets(self):
        for name, expected in [
            ("telegram_2026-10-16_130101_last14d.md", datetime(2026, 10, 16, 11, 1, 1, tzinfo=UTC)),
            ("telegram_2026-01-16_130101_last3d.md", datetime(2026, 1, 16, 12, 1, 1, tzinfo=UTC)),
        ]:
            with self.subTest(name=name):
                self.assertEqual(digest.read_export_end(self.legacy(name)), expected)

    def test_legacy_repeated_autumn_hour_chooses_earlier_instant(self):
        path = self.legacy("telegram_2026-10-25_023000_last1d.md")
        self.assertEqual(digest.read_export_end(path), datetime(2026, 10, 25, 0, 30, tzinfo=UTC))

    def test_new_utc_timestamp_resolves_repeated_autumn_hour_exactly(self):
        path = checkpoint(self.exports, "telegram_2026-10-25_023000_last1d.md",
                          "2026-10-25T01:30:00+00:00")
        self.assertEqual(digest.read_export_end(path), datetime(2026, 10, 25, 1, 30, tzinfo=UTC))

    def test_nonexistent_spring_local_time_is_ignored(self):
        self.legacy("telegram_2026-03-29_023000_last1d.md")
        self.legacy("telegram_2026-03-29_013000_last1d.md")
        self.assertEqual(digest.export_start(self.exports, None, NOW),
                         datetime(2026, 3, 29, 0, 30, tzinfo=UTC))

    def test_invalid_timestamps_do_not_override_valid_checkpoint(self):
        valid = NOW - timedelta(days=2)
        checkpoint(self.exports, "valid.md", valid.isoformat())
        for index, timestamp in enumerate([
            "not-a-date", "2026-02-30T12:00:00Z", "2026-10-17T11:00:00",
            "2026-10-17", "2026-10-17T11:00:00+02:00", "", "2099-01-01T00:00:00Z",
        ]):
            # These legacy-shaped names must not be used as fallback for bad metadata.
            checkpoint(self.exports, f"telegram_2026-10-17_11000{index}_last1d.md", timestamp)
        self.legacy("telegram_2026-02-30_110000_last1d.md")
        self.assertEqual(digest.export_start(self.exports, None, NOW), valid)

    def test_only_invalid_checkpoints_fall_back_to_24_hours(self):
        checkpoint(self.exports, "invalid.md", "not-a-date")
        self.assertEqual(digest.export_start(self.exports, None, NOW), NOW - timedelta(days=1))

    def test_missing_completion_marker_and_duplicate_metadata_are_ignored(self):
        checkpoint(self.exports, "telegram_2026-10-17_130000_last1d.md",
                   "2026-10-17T11:00:00Z", complete=False)
        path = checkpoint(self.exports, "duplicate.md", "2026-10-17T11:00:00Z")
        path.write_text(path.read_text(encoding="utf-8").replace(
            "Range: synthetic", "Export end (UTC): 2026-10-17T10:00:00Z"), encoding="utf-8")
        self.assertEqual(digest.export_start(self.exports, None, NOW), NOW - timedelta(days=1))

    def test_temporary_files_and_unrelated_markdown_are_not_checkpoints(self):
        checkpoint(self.exports, ".telegram_interrupted.tmp", "2026-10-17T11:00:00Z")
        self.legacy("notes.md")
        self.assertEqual(digest.export_start(self.exports, None, NOW), NOW - timedelta(days=1))

    def test_explicit_days_bypass_checkpoint_scanning(self):
        checkpoint(self.exports, "latest.md", "2026-10-17T11:00:00Z")
        with patch.object(digest, "read_export_end", side_effect=AssertionError("Must not scan")):
            for days in (1, 3, 7, 14):
                with self.subTest(days=days):
                    self.assertEqual(digest.export_start(self.exports, days, NOW),
                                     NOW - timedelta(hours=days * 24))

    def test_explicit_days_remain_24_hours_across_dst_changes(self):
        for now in (datetime(2026, 3, 29, 12, tzinfo=UTC), datetime(2026, 10, 25, 12, tzinfo=UTC)):
            with self.subTest(now=now):
                since = digest.export_start(self.exports, 1, now)
                self.assertEqual(now - since, timedelta(hours=24))
                self.assertNotEqual(now.astimezone(digest.ROME).hour,
                                    since.astimezone(digest.ROME).hour)

    def test_incremental_window_across_dst_uses_exact_previous_end(self):
        for start, end in [
            ("2026-03-28T12:00:00Z", datetime(2026, 3, 29, 12, tzinfo=UTC)),
            ("2026-10-24T12:00:00Z", datetime(2026, 10, 25, 12, tzinfo=UTC)),
        ]:
            with self.subTest(end=end):
                checkpoint(self.exports, "latest.md", start)
                self.assertEqual(digest.export_start(self.exports, None, end), end - timedelta(hours=24))

    def test_nonpositive_days_fail(self):
        for days in (0, -1):
            with self.subTest(days=days), self.assertRaisesRegex(SystemExit, "positive integer"):
                digest.export_start(self.exports, days, NOW)

    def test_checkpoint_read_failure_aborts_instead_of_advancing(self):
        self.legacy("telegram_2026-10-16_130101_last1d.md")
        with patch.object(digest, "read_export_end", side_effect=PermissionError("Synthetic denial")):
            with self.assertRaises(PermissionError):
                digest.export_start(self.exports, None, NOW)


class ExportTests(TemporaryExports):
    def setUp(self):
        super().setUp()
        FrozenDateTime.current = NOW
        self.client = FakeClient()
        self.patch("telegram_digest.ROOT", self.root)
        self.patch("telegram_digest.datetime", FrozenDateTime)
        self.patch("telegram_digest.credentials", return_value=(1, "synthetic-api-hash"))
        self.chats = self.patch("telegram_digest.selected_chats", return_value={"Synthetic course": -1})
        self.patch("telegram_digest.TelegramClient", return_value=self.client)

    def run_export(self, days=None):
        asyncio.run(digest.run(Namespace(command="export", days=days)))

    def files(self):
        return sorted(self.exports.glob("*.md"))

    def test_first_export_preserves_chronology_and_markdown(self):
        since = NOW - timedelta(days=1)
        self.client.messages[-1] = [
            message(5, NOW + timedelta(seconds=1)), message(4, NOW),
            message(3, NOW - timedelta(hours=1), "Synthetic first line\nsecond line"),
            message(2, since), message(1, since - timedelta(microseconds=1)),
        ]
        self.run_export()
        path, = self.files()
        text = path.read_text(encoding="utf-8")
        self.assertIn("Range: 2026-10-16 14:00 CEST — 2026-10-17 14:00 CEST", text)
        self.assertIn("Export end (UTC): 2026-10-17T12:00:00.123456+00:00", text)
        self.assertIn("Messages: 2", text)
        self.assertIn("**2026-10-17 13:00 CEST** · "
                      "[Original message](https://t.me/synthetic_course/3)\n\n"
                      "> Synthetic first line\n> second line\n", text)
        self.assertLess(text.index("synthetic_course/2"), text.index("synthetic_course/3"))
        self.assertIn("\n---\n", text)
        for mid in (1, 4, 5):
            self.assertNotIn(f"synthetic_course/{mid})", text)
        self.assertEqual(digest.read_export_end(path), NOW)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_incremental_export_can_reach_back_more_than_24_hours(self):
        previous = NOW - timedelta(days=4)
        checkpoint(self.exports, "previous.md", previous.isoformat())
        self.client.messages[-1] = [message(2, NOW - timedelta(days=2)), message(1, previous)]
        self.run_export()
        path, = [path for path in self.files() if path.name != "previous.md"]
        self.assertIn("Messages: 2", path.read_text(encoding="utf-8"))

    def test_legacy_export_resumes_at_filename_end_including_boundary(self):
        previous = self.legacy("telegram_2026-10-16_130101_last14d.md")
        original = previous.read_bytes()
        boundary = datetime(2026, 10, 16, 11, 1, 1, tzinfo=UTC)
        self.client.messages[-1] = [message(2, boundary), message(1, boundary - timedelta(seconds=1))]
        self.run_export()
        path, = [path for path in self.files() if path != previous]
        text = path.read_text(encoding="utf-8")
        self.assertIn("Range: 2026-10-16 13:01 CEST", text)
        self.assertIn("Messages: 1", text)
        self.assertIn("Synthetic message 2", text)
        self.assertEqual(previous.read_bytes(), original)

    def test_explicit_days_override_previous_export(self):
        checkpoint(self.exports, "previous.md", (NOW - timedelta(days=10)).isoformat())
        self.client.messages[-1] = [message(3, NOW - timedelta(days=3)),
                                    message(2, NOW - timedelta(days=4)),
                                    message(1, NOW - timedelta(days=5))]
        self.run_export(days=3)
        path, = [path for path in self.files() if path.name != "previous.md"]
        text = path.read_text(encoding="utf-8")
        self.assertIn("Messages: 1", text)
        self.assertIn("Synthetic message 3", text)
        self.assertNotIn("Synthetic message 2", text)
        self.assertIn("_last3d", path.name)
        self.assertEqual(digest.export_start(self.exports, None, NOW + timedelta(hours=1)), NOW)

    def test_empty_export_creates_checkpoint_used_by_next_run(self):
        self.run_export()
        first, = self.files()
        original = first.read_bytes()
        self.assertIn("No messages in range.", original.decode())
        self.assertEqual(digest.read_export_end(first), NOW)
        FrozenDateTime.current = NOW + timedelta(hours=1)
        self.run_export()
        second, = [path for path in self.files() if path != first]
        self.assertIn("Range: 2026-10-17 14:00 CEST — 2026-10-17 15:00 CEST",
                      second.read_text(encoding="utf-8"))
        self.assertEqual(digest.read_export_end(second), NOW + timedelta(hours=1))
        self.assertEqual(first.read_bytes(), original)

    def test_adjacent_exports_include_exact_boundary_once(self):
        boundary = NOW.replace(microsecond=0)
        FrozenDateTime.current = boundary
        self.client.messages[-1] = [message(4, boundary + timedelta(seconds=1)),
                                    message(3, boundary), message(2, boundary),
                                    message(1, boundary - timedelta(seconds=1))]
        self.run_export()
        first, = self.files()
        FrozenDateTime.current = boundary + timedelta(seconds=2)
        self.run_export()
        second, = [path for path in self.files() if path != first]
        first_text = first.read_text(encoding="utf-8")
        second_text = second.read_text(encoding="utf-8")
        self.assertIn("Messages: 1", first_text)
        self.assertIn("Messages: 3", second_text)
        for mid in (1, 2, 3, 4):
            self.assertEqual((first_text + second_text).count(f"synthetic_course/{mid})"), 1)

    def test_unrenderable_messages_still_allow_empty_checkpoint(self):
        self.client.messages[-1] = [message(2, None), message(1, NOW - timedelta(hours=1), body="")]
        self.run_export()
        path, = self.files()
        self.assertIn("Messages: 0", path.read_text(encoding="utf-8"))
        self.assertEqual(digest.read_export_end(path), NOW)

    def test_missing_configured_group_aborts_and_preserves_previous_export(self):
        previous = checkpoint(self.exports, "previous.md", (NOW - timedelta(days=2)).isoformat())
        original = previous.read_bytes()
        self.chats.return_value["Missing synthetic course"] = -2
        with self.assertRaisesRegex(SystemExit, "Cannot access all configured groups"):
            self.run_export()
        self.assertEqual(self.files(), [previous])
        self.assertEqual(previous.read_bytes(), original)

    def test_error_reading_second_group_does_not_publish_partial_export(self):
        self.client.dialogs.append(dialog(-2))
        self.chats.return_value["Other synthetic course"] = -2
        self.client.messages[-1] = [message(1, NOW - timedelta(hours=1))]
        self.client.messages[-2] = [PermissionError("Synthetic Telegram access error")]
        with self.assertRaises(PermissionError):
            self.run_export()
        self.assertEqual(self.files(), [])

    def test_interrupted_or_failed_message_fetch_does_not_advance_checkpoint(self):
        previous_end = NOW - timedelta(days=2)
        previous = checkpoint(self.exports, "previous.md", previous_end.isoformat())
        original = previous.read_bytes()
        for error in (RuntimeError("Synthetic network failure"), asyncio.CancelledError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.client.messages[-1] = [message(1, NOW - timedelta(hours=1)), error]
                with self.assertRaises(type(error)):
                    self.run_export()
                self.assertEqual(self.files(), [previous])
                self.assertEqual(previous.read_bytes(), original)
                self.assertEqual(digest.export_start(self.exports, None, NOW), previous_end)

    def test_disconnect_failure_does_not_publish_checkpoint(self):
        self.client.exit_error = RuntimeError("Synthetic disconnect failure")
        with self.assertRaises(RuntimeError):
            self.run_export()
        self.assertEqual(self.files(), [])

    def test_publication_happens_after_successful_disconnect(self):
        write = digest.write_export

        def verify_disconnected(*args):
            self.assertTrue(self.client.disconnected)
            return write(*args)

        with patch.object(digest, "write_export", side_effect=verify_disconnected):
            self.run_export()
        self.assertEqual(len(self.files()), 1)

    def test_write_failure_or_interrupt_leaves_no_markdown_or_temporary_file(self):
        for error in (OSError("Synthetic disk failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch.object(digest.os, "fsync", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.run_export()
                self.assertEqual(list(self.exports.iterdir()), [])

    def test_atomic_publication_failure_preserves_previous_checkpoint(self):
        previous_end = NOW - timedelta(days=2)
        previous = checkpoint(self.exports, "previous.md", previous_end.isoformat())
        original = previous.read_bytes()
        with patch.object(digest.os, "link", side_effect=OSError("Synthetic publication failure")):
            with self.assertRaises(OSError):
                self.run_export()
        self.assertEqual(list(self.exports.iterdir()), [previous])
        self.assertEqual(previous.read_bytes(), original)
        self.assertEqual(digest.export_start(self.exports, None, NOW), previous_end)

    def test_same_second_exports_never_overwrite_existing_files(self):
        self.run_export(days=1)
        first, = self.files()
        original = first.read_bytes()
        original_stat = first.stat()
        self.run_export(days=1)
        self.assertEqual(len(self.files()), 2)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(first.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(first.stat().st_ino, original_stat.st_ino)

    def test_list_command_still_works_without_export_arguments(self):
        asyncio.run(digest.run(Namespace(command="list")))
        self.chats.assert_not_called()
        self.assertFalse(self.exports.exists())

    def test_invalid_days_fail_before_authentication(self):
        with self.assertRaisesRegex(SystemExit, "positive integer"):
            self.run_export(days=0)
        digest.credentials.assert_not_called()


class CommandAndPrivacyTests(unittest.TestCase):
    def test_cli_default_is_incremental_and_explicit_days_are_preserved(self):
        self.assertIsNone(digest.parse_args(["export"]).days)
        for days in (1, 3, 7):
            self.assertEqual(digest.parse_args(["export", "--days", str(days)]).days, days)

    def test_sensitive_paths_are_ignored_by_git(self):
        paths = [".env", ".env.local", "chats.json", "exports/synthetic.md",
                 "exports/.telegram_synthetic.tmp", "secrets/synthetic.session",
                 "synthetic.session", "synthetic.session-journal",
                 "synthetic.session-wal", "synthetic.session-shm"]
        result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                                input="\n".join(paths) + "\n", text=True, capture_output=True,
                                cwd=Path(__file__).resolve().parents[1], check=True)
        self.assertEqual(result.stdout.splitlines(), paths)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_documented_zsh_shortcut_passes_correct_arguments(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        function = readme.split("```zsh\n", 1)[1].split("\n```", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "synthetic project"
            executable = project / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n', encoding="utf-8")
            executable.chmod(0o700)
            function = function.replace('local project="$HOME/Projects/telegram-academic-digest"',
                                        f"local project={shlex.quote(str(project))}")
            for argument, expected in [("", []), ("1", ["--days", "1"]),
                                       ("3", ["--days", "3"]), ("7", ["--days", "7"])]:
                with self.subTest(argument=argument):
                    result = subprocess.run(
                        [shutil.which("zsh"), "-df", "-c", function + "\ndigest " + argument],
                        cwd=temporary, env={"PATH": os.defpath, "ZDOTDIR": temporary},
                        text=True, capture_output=True, check=True,
                    )
                    self.assertEqual(result.stdout.splitlines(),
                                     [str(project / "telegram_digest.py"), "export"] + expected)


if __name__ == "__main__":
    unittest.main()
