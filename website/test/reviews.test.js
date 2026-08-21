const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { reviewCountBySlug } = require("../src/_data/books");
const { isPublicReview, loadReviewsFrom } = require("../src/_data/reviews");

function scratchCorpus() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rwbc-public-reviews-"));
  fs.mkdirSync(path.join(root, "members"));
  fs.mkdirSync(path.join(root, "reviews"));
  fs.writeFileSync(
    path.join(root, "members", "reader.json"),
    JSON.stringify({ name: "Reader", isCurrent: true })
  );
  return root;
}

function writeReview(root, name, { dnf, rating, body }) {
  fs.writeFileSync(
    path.join(root, "reviews", name),
    `---\nbook: example-book\nmember: reader\nrating: ${rating ?? "null"}\ndnf: ${dnf}\ncreatedAt: '2026-08-21T00:00:00Z'\n---\n\n${body}\n`
  );
}

test("the public review loader excludes DNF records and their prose", () => {
  const root = scratchCorpus();
  writeReview(root, "example-book--reader.md", {
    dnf: true,
    rating: null,
    body: "Private reason for not finishing.",
  });
  writeReview(root, "example-book--other.md", {
    dnf: false,
    rating: 4,
    body: "Public review.",
  });

  const reviews = loadReviewsFrom(root);
  assert.equal(reviews.length, 1);
  assert.equal(reviews[0].review, "Public review.");
  assert.equal(Object.hasOwn(reviews[0], "dnf"), false);
  assert.equal(reviewCountBySlug(root).get("example-book"), 1);
});

test("the publication rule fails closed unless dnf is explicitly false", () => {
  assert.equal(isPublicReview({ dnf: false }), true);
  assert.equal(isPublicReview({ dnf: true }), false);
  assert.equal(isPublicReview({}), false);
  assert.equal(isPublicReview({ dnf: "false" }), false);
});
