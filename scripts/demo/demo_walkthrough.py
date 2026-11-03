"""The demo's walk-through in a real browser (Playwright), optionally recorded.

Each step runs in its own browser context, so waiting for a run between steps is off camera; the
sign-in carries over. A step opens with a card that names it and says how long the wait before
it took. Captions are drawn as a subtitle bar, styled apart from the product, and say what is
on screen. Nothing on the page is altered or staged.
"""

import html
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from playwright.sync_api import Browser, Locator, Page, StorageState, expect, sync_playwright

DASHBOARD = "http://127.0.0.1:3000"
HOLD = 2500
STARTS = 30_000
"""Milliseconds to allow a decision that starts a run, which can take longer than a page."""
"""Milliseconds to hold on each thing worth reading."""
REHEARSAL = "Local rehearsal · GitHub is simulated · DataProof is real · nothing is sped up"
LIVE = "Live run · a real GitHub pull request · DataProof on one machine · nothing is sped up"
CARD = """<!doctype html><html><body style="margin:0;height:100vh;display:flex;
flex-direction:column;justify-content:center;padding:0 96px;background:#0f172a;color:#f8fafc;
font:20px/1.5 system-ui,sans-serif"><p style="color:#93c5fd;margin:0">{kicker}</p>
<h1 style="font-size:40px;margin:12px 0 24px">{title}</h1><p style="max-width:900px">{text}</p>
<p style="position:fixed;bottom:24px;color:#94a3b8;font-size:15px">{footer}</p></body></html>"""
STARTED = """previous => {
  const found = /A new run started: ([0-9a-f-]{36})/.exec(
    document.querySelector('[role="status"]')?.textContent ?? "");
  return found && found[1] !== previous ? found[1] : null;
}"""
"""The run an approval started, once the notice names one other than ``previous``'s."""
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


def scroll_to(target: Locator) -> None:
    """Bring an element to the middle of the view at once.

    Playwright's own scrolling waits for the element to stop moving, and the dashboard
    refreshes while a run is open, which held a step still for seconds.
    """
    target.evaluate("element => element.scrollIntoView({block: 'center'})")


def card_start(ffmpeg: str, segment: Path) -> float:
    """Seconds into a recording at which its dark card is drawn: before it, the page is blank."""
    probe = subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-i",
            str(segment),
            "-t",
            "8",
            "-an",
            "-vf",
            "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    frames = re.findall(r"pts_time:([0-9.]+)[^\n]*\n[^\n]*YAVG=([0-9.]+)", probe.stderr)
    return next((float(time) for time, luma in frames if float(luma) < 80), 0.0)


def short(run_id: UUID) -> str:
    return str(run_id)[:8]


class Walkthrough:
    """The walk-through. ``state`` is a dashboard session made before recording (the live demo
    signs in with real GitHub off camera); without it, step 1 signs in on camera."""

    def __init__(
        self,
        video_dir: Path | None,
        size: dict[str, int],
        *,
        footer: str = REHEARSAL,
        state: StorageState | None = None,
        reviewer: str = "maintainer",
    ) -> None:
        self._playwright = sync_playwright().start()
        self._browser: Browser = self._playwright.chromium.launch()
        self._state = state
        self._video_dir = video_dir
        self._size = size
        self._footer = footer
        self._reviewer = reviewer
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
                footer=html.escape(self._footer),
            )
        )
        page.wait_for_timeout(3000)
        try:
            yield page
        finally:
            self._state = context.storage_state()
            video = page.video
            context.close()
            if video is not None:
                self.segments.append(Path(video.path()))

    def _github(self, page: Page, url: str, text: str, hold: int = 4000) -> None:
        """One of GitHub's own pages, as anyone can see it: the demo repository is public."""
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        caption(page, text, hold)

    def _open_run(self, page: Page, run_id: UUID, verdict: str) -> None:
        page.goto(f"{DASHBOARD}/runs/{run_id}")
        expect(page.locator("h1")).to_contain_text(verdict)

    def _preview_origins(self, page: Page) -> dict[str, str]:
        """Open both previews from the run page; the origin each one's grant opened."""
        origins = {}
        for role in ("reference", "candidate"):
            with page.expect_popup() as popup:
                page.get_by_role("button", name=f"Open {role} preview").click()
            preview = popup.value
            preview.wait_for_load_state()
            scheme, _, host = preview.url.split("/", 3)[:3]
            origins[role] = f"{scheme}//{host}"
            preview.close()
        return origins

    def _show_order(self, page: Page, origin: str, order: int, text: str) -> None:
        page.goto(f"{origin}/orders/{order}")
        expect(page.get_by_test_id("refunded-total")).to_be_visible()
        caption(page, text, hold=3000)

    def _approve_every_binding(self, page: Page, comment: str, explain: str) -> UUID:
        """Approve each binding awaiting review on this run; the run the last approval started.

        Each approval starts a run that carries every approval at that commit, so the last one
        carries them all. Each click waits for the notice to name its own run: the notice still
        names the previous approval's until the new one is recorded.
        """
        reviews = page.locator("section.binding")
        count = reviews.count()
        started: str | None = None
        for index in range(count):
            review = reviews.nth(index)
            scroll_to(review)
            if index == 0:
                caption(page, explain.format(count=count), hold=4000)
                review.get_by_label("Comment (optional)").fill(comment)
            review.get_by_role("button", name="Approve binding").click()
            started = page.wait_for_function(STARTED, arg=started, timeout=STARTS).json_value()
            reviews = page.locator("section.binding")
        if started is None:
            raise RuntimeError("the run has no binding awaiting review")
        return UUID(started)

    def binding_review(self, run_id: UUID, waited: float, pull_request: str | None = None) -> UUID:
        """Step 1; the run that carries the reviewer's approvals."""
        with self._step(
            "Step 1 of 4",
            "A pull request rewrites the payments ledger",
            f"A signed pull_request webhook started run {short(run_id)}. A worker fetched the "
            "exact commits, migrated isolated copies of the database, built previews of both "
            f"commits and ran the protected contracts: {waited:.0f} s, off camera.",
        ) as page:
            if pull_request is not None:
                self._github(
                    page,
                    pull_request,
                    "The pull request, on GitHub: a migration that moves legacy payment events "
                    "into a new per-order ledger.",
                )
                self._github(
                    page,
                    f"{pull_request}/checks",
                    "DataProof's check on the pull request's head commit, published by the "
                    "DataProof GitHub App.",
                    hold=4500,
                )
            page.goto(f"{DASHBOARD}/")
            if self._state is None:
                page.get_by_role("link", name="Sign in with GitHub").click()
                expect(page.get_by_text(f"Signed in as {self._reviewer}")).to_be_visible()
                caption(page, "The reviewer signs in with GitHub (simulated here).", hold=1500)
            else:
                expect(page.get_by_text(f"Signed in as {self._reviewer}")).to_be_visible()
                caption(
                    page,
                    f"The reviewer signed in with GitHub as {self._reviewer} before recording, "
                    "so no credentials are on screen.",
                    hold=2500,
                )
            page.get_by_role("link", name=short(run_id)).click()
            expect(page.locator("h1")).to_contain_text("INCOMPLETE")
            caption(
                page,
                "INCOMPLETE, so the required GitHub check is not green: nothing the pull "
                "request brings is trusted until a reviewer approves it.",
                hold=3500,
            )
            approval = self._approve_every_binding(
                page,
                "Reads the new ledger table.",
                "The pull request proposes {count} bindings: how its new ledger maps onto each "
                "protected contract. The reviewer reads each one's SQL and what it reads, then "
                "approves it.",
            )
            caption(page, "Each approval is recorded; one new run carries all of them.", 2000)
        return approval

    def blocked(self, run_id: UUID, waited: float, correction: str) -> None:
        with self._step(
            "Step 2 of 4",
            "The migration succeeded. The data did not.",
            f"Run {short(run_id)} checked the change under the approved bindings: {waited:.0f} s, "
            "off camera.",
        ) as page:
            self._open_run(page, run_id, "BLOCKED")
            caption(page, "BLOCKED: the migration ran without error, yet contracts fail.", 2500)
            row = page.locator("article.check.fail tr", has_text="1002").first
            scroll_to(row)
            caption(
                page,
                "Refund event 1002 is 49.99 in the reference and 99.98 in the candidate. Every "
                "changed value is a whole multiple of the original.",
                hold=1500,
            )
            origins = self._preview_origins(page)  # the caption stays up meanwhile
            page.wait_for_timeout(1500)
            run_page = page.url
            self._show_order(
                page,
                origins["reference"],
                1,
                "The reference preview (base commit, its own host and database): order 1 "
                "refunded 49.99.",
            )
            self._show_order(
                page,
                origins["candidate"],
                1,
                "The candidate preview (head commit after the migration): the same order now "
                "shows the doubled refund.",
            )
            page.goto(run_page)
            scroll_to(page.get_by_role("heading", name="Findings"))
            caption(
                page,
                "The diagnosis cites its evidence: the rule-based, model-off one, since no AI "
                "model is called in this demo. Only the checks decide the verdict.",
                hold=4500,
            )
            page.get_by_label("Correction commit").fill(correction)
            page.get_by_role("button", name="Replay a correction").click()
            expect(page.get_by_role("status")).to_contain_text(
                "Correction run started", timeout=STARTS
            )
            caption(page, "The reviewer replays the corrected commit on the same baseline.", 2000)

    def correction_review(self, run_id: UUID, waited: float) -> UUID:
        """Step 3; the run that carries the approvals of the corrected commit's bindings."""
        with self._step(
            "Step 3 of 4",
            "The corrected commit proposes its own bindings",
            f"Correction run {short(run_id)} finished in {waited:.0f} s, off camera. An approval "
            "is bound to one commit and one binding digest, so nothing carries over silently.",
        ) as page:
            self._open_run(page, run_id, "INCOMPLETE")
            return self._approve_every_binding(
                page,
                "Same ledger mapping on the corrected commit.",
                "INCOMPLETE again: the reviewer approves the corrected commit's {count} bindings.",
            )

    def checked(self, run_id: UUID, waited: float, github: Sequence[tuple[str, str]] = ()) -> None:
        """The CHECKED run, then each ``(url, caption)`` of GitHub's own pages."""
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
                hold=4000,
            )
            for url, text in github:
                self._github(page, url, text, hold=4500)

    def join(self, out: Path) -> Path:
        """One MP4 of every step, in order: needs ffmpeg on the path, or ``FFMPEG``.

        Each recording starts blank until its card is drawn; that is skipped.
        """
        ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SystemExit(f"no ffmpeg to join the steps; they are {self.segments}")
        inputs: list[str] = []
        for segment in self.segments:  # seeking before each input is frame-accurate
            inputs += ["-ss", f"{card_start(ffmpeg, segment):.2f}", "-i", str(segment)]
        streams = "".join(f"[{index}:v]" for index in range(len(self.segments)))
        subprocess.run(  # noqa: S603
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                *inputs,
                "-filter_complex",
                f"{streams}concat=n={len(self.segments)}:v=1:a=0[video]",
                "-map",
                "[video]",
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
