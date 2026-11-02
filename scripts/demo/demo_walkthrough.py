"""The demo's walk-through in a real browser (Playwright), optionally recorded.

Each step runs in its own browser context, so waiting for a run between steps is off camera; the
sign-in carries over. A step opens with a card that names it and says how long the wait before
it took. Captions are drawn as a subtitle bar, styled apart from the product, and say what is
on screen. Nothing on the page is altered or staged.
"""

import html
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from playwright.sync_api import Browser, Page, StorageState, expect, sync_playwright

DASHBOARD = "http://127.0.0.1:3000"
HOLD = 2500
STARTS = 30_000
"""Milliseconds to allow a decision that starts a run, which can take longer than a page."""
"""Milliseconds to hold on each thing worth reading."""
FOOTER = "Local rehearsal · GitHub is simulated · DataProof is real · nothing is sped up"
CARD = """<!doctype html><html><body style="margin:0;height:100vh;display:flex;
flex-direction:column;justify-content:center;padding:0 96px;background:#0f172a;color:#f8fafc;
font:20px/1.5 system-ui,sans-serif"><p style="color:#93c5fd;margin:0">{kicker}</p>
<h1 style="font-size:40px;margin:12px 0 24px">{title}</h1><p style="max-width:900px">{text}</p>
<p style="position:fixed;bottom:24px;color:#94a3b8;font-size:15px">{footer}</p></body></html>"""
CAPTION = """text => {
  let bar = document.getElementById("__demo_caption");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "__demo_caption";
    bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:2147483647;" +
      "padding:12px 24px;background:rgba(15,23,42,.92);color:#f8fafc;" +
      "font:18px/1.4 system-ui,sans-serif;border-top:3px solid #93c5fd";
    document.body.appendChild(bar);
  }
  bar.textContent = "▶ " + text;
}"""


def caption(page: Page, text: str, hold: int = HOLD) -> None:
    page.evaluate(CAPTION, text)
    page.wait_for_timeout(hold)


def short(run_id: UUID) -> str:
    return str(run_id)[:8]


class Walkthrough:
    def __init__(self, video_dir: Path | None, size: dict[str, int]) -> None:
        self._playwright = sync_playwright().start()
        self._browser: Browser = self._playwright.chromium.launch()
        self._state: StorageState | None = None
        self._video_dir = video_dir
        self._size = size
        self.segments: list[Path] = []

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()

    @contextmanager
    def _step(self, kicker: str, title: str, text: str) -> Iterator[Page]:
        options: dict[str, Any] = {"viewport": self._size}
        if self._state is not None:
            options["storage_state"] = self._state
        if self._video_dir is not None:
            options["record_video_dir"] = str(self._video_dir)
            options["record_video_size"] = self._size
        context = self._browser.new_context(**options)
        page = context.new_page()
        page.set_content(
            CARD.format(
                kicker=html.escape(kicker),
                title=html.escape(title),
                text=html.escape(text),
                footer=html.escape(FOOTER),
            )
        )
        page.wait_for_timeout(3500)
        try:
            yield page
        finally:
            self._state = context.storage_state()
            video = page.video
            context.close()
            if video is not None:
                self.segments.append(Path(video.path()))

    def _open_run(self, page: Page, run_id: UUID, verdict: str) -> None:
        page.goto(f"{DASHBOARD}/runs/{run_id}")
        expect(page.locator("h1")).to_contain_text(verdict)

    def _preview(self, page: Page, role: str, order: int, text: str) -> None:
        """Open a preview from the run page, show one order on it, and come back."""
        run_page = page.url
        with page.expect_popup() as popup:
            page.get_by_role("button", name=f"Open {role} preview").click()
        preview = popup.value
        preview.wait_for_load_state()
        scheme, _, host = preview.url.split("/", 3)[:3]
        preview.close()
        page.goto(f"{scheme}//{host}/orders/{order}")
        expect(page.get_by_test_id("refunded-total")).to_be_visible()
        caption(page, text, hold=3500)
        page.goto(run_page)
        expect(page.locator("h1")).to_be_visible()

    def _approve_every_binding(self, page: Page, comment: str) -> int:
        """Approve each binding awaiting review on this run; how many there were."""
        reviews = page.locator("section.binding")
        count = reviews.count()
        for index in range(count):
            review = reviews.nth(index)
            review.scroll_into_view_if_needed()
            if index == 0:
                caption(
                    page,
                    f"The pull request proposes {count} bindings: how its new ledger maps onto "
                    "each protected contract. The reviewer reads each one's SQL and the "
                    "relations it reads, then approves it.",
                    hold=5000,
                )
                review.get_by_label("Comment (optional)").fill(comment)
            review.get_by_role("button", name="Approve binding").click()
            expect(page.get_by_role("status")).to_contain_text("Approved", timeout=STARTS)
            reviews = page.locator("section.binding")
        return count

    def binding_review(self, run_id: UUID, waited: float) -> None:
        with self._step(
            "Step 1 of 4",
            "A pull request rewrites the payments ledger",
            f"A signed pull_request webhook started run {short(run_id)}. A worker fetched the "
            "exact commits, migrated isolated copies of the database, built previews of both "
            f"commits and ran the protected contracts: {waited:.0f} s, off camera.",
        ) as page:
            page.goto(f"{DASHBOARD}/")
            page.get_by_role("link", name="Sign in with GitHub").click()
            expect(page.get_by_text("Signed in as maintainer")).to_be_visible()
            caption(page, "The reviewer signs in with GitHub (simulated here).", hold=2000)
            page.get_by_role("link", name=short(run_id)).click()
            expect(page.locator("h1")).to_contain_text("INCOMPLETE")
            caption(
                page,
                "INCOMPLETE, so the required GitHub check is not green: nothing the pull "
                "request brings is trusted until a reviewer approves it.",
                hold=4000,
            )
            self._approve_every_binding(page, "Reads the new ledger table.")
            caption(page, "Each approval is recorded; one new run carries all of them.", 2500)

    def blocked(self, run_id: UUID, waited: float, correction: str) -> None:
        with self._step(
            "Step 2 of 4",
            "The migration succeeded. The data did not.",
            f"Run {short(run_id)} checked the change under the approved bindings: {waited:.0f} s, "
            "off camera.",
        ) as page:
            self._open_run(page, run_id, "BLOCKED")
            caption(page, "BLOCKED: the migration ran without error, yet contracts fail.", 3000)
            row = page.locator("article.check.fail tr", has_text="1002").first
            row.scroll_into_view_if_needed()
            caption(
                page,
                "Refund event 1002 is 49.99 in the reference and 99.98 in the candidate. Every "
                "changed value is a whole multiple of the original.",
                hold=4500,
            )
            self._preview(
                page,
                "reference",
                1,
                "The reference preview (base commit, its own host and database): order 1 "
                "refunded 49.99.",
            )
            self._preview(
                page,
                "candidate",
                1,
                "The candidate preview (head commit after the migration): the same order now "
                "shows the doubled refund.",
            )
            page.get_by_role("heading", name="Findings").scroll_into_view_if_needed()
            caption(
                page,
                "The diagnosis cites its evidence: here the rule-based, model-off one, since no "
                "AI model is called in this rehearsal. Only the checks decide the verdict.",
                hold=5000,
            )
            page.get_by_label("Correction commit").fill(correction)
            page.get_by_role("button", name="Replay a correction").click()
            expect(page.get_by_role("status")).to_contain_text(
                "Correction run started", timeout=STARTS
            )
            caption(page, "The reviewer replays the corrected commit on the same baseline.", 2500)

    def correction_review(self, run_id: UUID, waited: float) -> None:
        with self._step(
            "Step 3 of 4",
            "The corrected commit proposes its own bindings",
            f"Correction run {short(run_id)} finished in {waited:.0f} s, off camera. An approval "
            "is bound to one commit and one binding digest, so nothing carries over silently.",
        ) as page:
            self._open_run(page, run_id, "INCOMPLETE")
            self._approve_every_binding(page, "Same ledger mapping on the corrected commit.")

    def checked(self, run_id: UUID, waited: float) -> None:
        with self._step(
            "Step 4 of 4",
            "The corrected migration is checked",
            f"Run {short(run_id)} finished in {waited:.0f} s, off camera.",
        ) as page:
            self._open_run(page, run_id, "CHECKED")
            caption(
                page,
                "CHECKED: every protected contract passes on the corrected commit, and the "
                "result is published as that commit's GitHub check.",
                hold=4500,
            )
            page.get_by_role("heading", name="Coverage").scroll_into_view_if_needed()
            caption(page, "The coverage table lists every contract and its outcome.", 3500)

    def join(self, out: Path) -> Path:
        """One MP4 of every step, in order: needs ffmpeg on the path, or ``FFMPEG``.

        Each recording starts with a blank frame before its card is drawn; that is skipped.
        """
        ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SystemExit(f"no ffmpeg to join the steps; they are {self.segments}")
        listing = out.with_suffix(".txt")
        entries = [f"file '{p.as_posix()}'\ninpoint 0.8\n" for p in self.segments]
        listing.write_text("".join(entries), "utf-8")
        subprocess.run(  # noqa: S603
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c:v",
                "libx264",
                "-crf",
                "26",
                "-preset",
                "slow",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                str(out),
            ],
            check=True,
        )
        return out
