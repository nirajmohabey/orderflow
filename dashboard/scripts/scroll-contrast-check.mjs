/** Computed contrast and keyboard access for axe's clipped/SVG incomplete nodes.
 * Actual built frontend, fully intercepted APIs. Explicit scratch output required.
 * node scripts/scroll-contrast-check.mjs <scratch-output-directory>
 */
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import AxeBuilder from "@axe-core/playwright";

assert.ok(process.argv[2], "Provide an explicit scratch output directory");
process.env.ORDERFLOW_BROWSER_OUT = path.resolve(process.argv[2]);
const { scenario, finishHarness, OUT } = await import("./browser-check.mjs");
const observations = [],
  scans = [],
  keyboard = [];
const luminance = (rgb) =>
  rgb
    .slice(0, 3)
    .map((c) => c / 255)
    .map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
    .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
const rgb = (color) => {
  const v = color.match(/[\d.]+/g)?.map(Number);
  assert.ok(v, `Unsupported color ${color}`);
  return [v[0], v[1], v[2], v[3] ?? 1];
};
async function inspect(page, state) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  scans.push({
    state,
    violations: results.violations,
    incomplete: results.incomplete,
  });
  assert.equal(results.violations.length, 0, `Axe violations in ${state}`);
  for (const node of results.incomplete
    .filter((rule) => rule.id === "color-contrast")
    .flatMap((rule) => rule.nodes)) {
    assert.equal(
      node.target.length,
      1,
      "Unexpected nested incomplete target requires manual review",
    );
    const selector = node.target[0],
      locator = page.locator(selector);
    assert.equal(
      await locator.count(),
      1,
      `Recreate exact incomplete target ${selector}`,
    );
    await locator.scrollIntoViewIfNeeded();
    const measurements = await locator.evaluate((element) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT),
        records = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (!node.textContent.trim()) continue;
        const parent = node.parentElement,
          style = getComputedStyle(parent),
          range = document.createRange();
        range.selectNodeContents(node);
        if (
          style.display === "none" ||
          style.visibility !== "visible" ||
          !range.getClientRects().length
        )
          continue;
        const chain = [];
        for (let el = parent; el; el = el.parentElement) {
          const css = getComputedStyle(el);
          chain.push({
            background: css.backgroundColor,
            image: css.backgroundImage,
            opacity: css.opacity,
          });
        }
        records.push({
          text: node.textContent.trim(),
          foreground:
            parent.namespaceURI === "http://www.w3.org/2000/svg"
              ? style.fill
              : style.color,
          font_size: style.fontSize,
          font_weight: style.fontWeight,
          chain,
        });
      }
      return records;
    });
    assert.ok(measurements.length, `Text must be recreated for ${selector}`);
    for (const measurement of measurements) {
      assert.ok(
        measurement.chain.every(
          (css) => css.image === "none" && css.opacity === "1",
        ),
        "Complex background/opacity requires manual measurement",
      );
      let background = [255, 255, 255];
      for (const css of [...measurement.chain].reverse()) {
        const color = rgb(css.background);
        background = background.map(
          (value, i) => color[i] * color[3] + value * (1 - color[3]),
        );
      }
      const color = rgb(measurement.foreground),
        foreground = background.map(
          (value, i) => color[i] * color[3] + value * (1 - color[3]),
        );
      const a = luminance(foreground),
        b = luminance(background),
        contrast = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      const minimum =
        parseFloat(measurement.font_size) >= 24 ||
        (parseFloat(measurement.font_size) >= 18.667 &&
          parseFloat(measurement.font_weight) >= 700)
          ? 3
          : 4.5;
      observations.push({
        state,
        selector,
        ...measurement,
        effective_background: background,
        contrast,
        minimum,
        passed: contrast >= minimum,
      });
      assert.ok(
        contrast >= minimum,
        `${state}: ${selector} contrast ${contrast} below ${minimum}`,
      );
    }
  }
}
const views = [
  "Overview",
  "Current orders",
  "Products & history",
  "Event inbox",
  "Quarantines",
  "Runs & reports",
  "Inventory diff",
];
try {
  await scenario(
    "SVG history labels have contrast and exact table alternatives",
    async ({ page, go }) => {
      await go("Products & history");
      await page.locator(".record-link").first().click();
      await page.getByRole("button", { name: /Raw revisions/ }).waitFor();
      await inspect(page, "product history");
      assert.match(
        await page.locator(".history-chart svg").getAttribute("aria-label"),
        /Exact values.*following table/,
      );
      assert.ok(
        (await page.getByRole("dialog").locator("table tbody tr").count()) >= 2,
      );
    },
  );
  for (const view of views) {
    await scenario(
      `Clipped contrast targets and keyboard scroll: ${view}`,
      async ({ page, go }) => {
        await page.emulateMedia({ reducedMotion: "reduce" });
        await page.setViewportSize({ width: 375, height: 1000 });
        await go(view);
        await page.waitForFunction(() => !document.querySelector(".loading"));
        await inspect(page, `375 ${view}`);
        for (const region of await page.locator(".table-wrap").all()) {
          if (!(await region.isVisible())) continue;
          if (
            !(await region.evaluate(
              (el) => el.scrollWidth > el.clientWidth + 1,
            ))
          )
            continue;
          assert.equal(await region.getAttribute("tabindex"), "0");
          assert.equal(await region.getAttribute("role"), "region");
          assert.ok(await region.getAttribute("aria-label"));
          await region.evaluate((el) => {
            el.scrollLeft = 0;
          });
          await region.focus();
          for (let i = 0; i < 8; i++) await page.keyboard.press("ArrowRight");
          await page.waitForTimeout(120);
          const scroll = await region.evaluate((el) => ({
            left: el.scrollLeft,
            active: document.activeElement === el,
            outline: getComputedStyle(el).outlineStyle,
          }));
          keyboard.push({ state: view, ...scroll });
          assert.ok(
            scroll.active && scroll.left > 0,
            "Focused data region must scroll with arrow keys",
          );
          assert.notEqual(
            scroll.outline,
            "none",
            "Focused data region must have a visible focus indicator",
          );
        }
        const lastLink = page
          .getByRole("navigation", { name: "Main navigation" })
          .getByRole("link")
          .last();
        await page
          .getByRole("navigation", { name: "Main navigation" })
          .getByRole("link")
          .nth(views.length - 2)
          .focus();
        await page.keyboard.press("Tab");
        assert.ok(
          await lastLink.evaluate((el) => el === document.activeElement),
          "Tab reaches the final navigation link",
        );
        await page.waitForTimeout(150);
        const navGeometry = await lastLink.evaluate((el) => {
          const range = document.createRange();
          range.selectNodeContents(el.querySelector("span"));
          const r = range.getBoundingClientRect(),
            parent = el.parentElement.getBoundingClientRect();
          return {
            left: r.left,
            right: r.right,
            parent_left: parent.left,
            parent_right: parent.right,
            scroll: el.parentElement.scrollLeft,
          };
        });
        keyboard.push({ state: view, navigation: navGeometry });
        assert.ok(
          navGeometry.left >= navGeometry.parent_left - 1 &&
            navGeometry.right <= navGeometry.parent_right + 1,
          `Keyboard focus must reveal offscreen navigation labels: ${JSON.stringify(navGeometry)}`,
        );
      },
    );
  }
} finally {
  await finishHarness(views.length + 1);
  const report = JSON.parse(
    await fs.readFile(path.join(OUT, "browser-results.json"), "utf8"),
  );
  await fs.writeFile(
    path.join(OUT, "contrast-results.json"),
    JSON.stringify(
      {
        recorded_at: new Date().toISOString(),
        scope:
          "Real built frontend and CSS; all API responses intercepted. All contrast-incomplete targets in the recreated states measured from text-node computed colors after scrolling. No screenshot-pixel or full assistive-technology claim.",
        source_hashes: report.source_hashes,
        source_stable: report.source_stable,
        scans,
        observations,
        keyboard,
      },
      null,
      2,
    ) + "\n",
  );
}
