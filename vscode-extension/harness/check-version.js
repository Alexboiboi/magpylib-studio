/**
 * package.json's version and the changelog's newest section must agree.
 *
 *   node harness/check-version.js
 *
 * The engine cannot drift: pyproject sets `version.source = "vcs"`, so its
 * version *is* the git tag. The extension's is hand-written, and drifted —
 * v0.1.0, v0.1.0rc2 and v0.1.1 were all tagged with package.json still saying
 * 0.0.1, because release.yml syncs it in the runner and never commits that
 * back. Every local F5 and hand-rolled `vsce package` in that whole period
 * produced a 0.0.1 build, and the changelog shipped its notes under
 * "Unreleased" twice for the same reason: three files, moved by hand, one at
 * a time.
 *
 * So this pins the two the tag cannot reach. Preparing a release means
 * renaming the [Unreleased] heading to the new version and bumping
 * package.json in the same commit; anything else fails the next build. The
 * tag is then checked against both by release.yml.
 */
const fs = require("fs");
const path = require("path");

const EXT = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(EXT, "package.json"), "utf8"));
const changelog = fs.readFileSync(path.join(EXT, "CHANGELOG.md"), "utf8");

// Every `## [x.y.z]`, newest first — [Unreleased] is deliberately not one of
// them, so work in progress has somewhere to go that does not move the version.
const released = [...changelog.matchAll(/^## \[(\d+\.\d+\.\d+)\]/gm)].map(
  (m) => m[1],
);

const problems = [];

if (!released.length) {
  problems.push("CHANGELOG.md has no `## [x.y.z]` section at all");
} else if (released[0] !== pkg.version) {
  problems.push(
    `package.json says ${pkg.version}, CHANGELOG.md's newest section is ` +
      `[${released[0]}]. Bump both together: rename [Unreleased] to the new ` +
      `version and set package.json to match.`,
  );
}

// A release that is older than one already written up means the sections were
// added out of order, which silently decides what the next tag ships.
const descending = [...released].sort((a, b) =>
  b.localeCompare(a, undefined, { numeric: true }),
);
if (released.join() !== descending.join()) {
  problems.push(
    `CHANGELOG.md's versions are not newest-first: ${released.join(", ")}`,
  );
}

// Two sections for one version pass both checks above — the sort is stable, so
// duplicates stay in order — while only the first of them ever gets read.
const duplicated = released.filter((v, i) => released.indexOf(v) !== i);
if (duplicated.length) {
  problems.push(
    `CHANGELOG.md has more than one section for ${[...new Set(duplicated)].join(", ")}`,
  );
}

if (problems.length) {
  for (const problem of problems) console.log(`FAIL  ${problem}`);
  process.exit(1);
}
console.log(
  `ok    version ${pkg.version} matches the changelog ` +
    `(${released.length} released ${released.length === 1 ? "section" : "sections"})`,
);
