// Reviews are canonical corpus data: one Markdown file per review with slug-based
// frontmatter (book + member slugs) and the prose body. DNF is private selection/taste
// evidence, so this module is the publication boundary: only explicitly non-DNF
// records may reach Eleventy templates or public exports. Reviewer name/slug is
// derived from the member here so templates keep rendering names. Joined to books
// and members by slug (see the reviewsForBook / reviewsByMember filters).

const fs = require("fs");
const path = require("path");
const matter = require("gray-matter");
const corpus = require("../../lib/corpus");

const DATA = corpus.dataDir();

function isPublicReview(data) {
  // Fail closed: corpus validation requires this boolean, so a missing or malformed
  // value must not accidentally make a private record public.
  return data?.dnf === false;
}

function loadReviewsFrom(dataRoot) {
  const reviewsDir = path.join(dataRoot, "reviews");
  const membersDir = path.join(dataRoot, "members");
  if (!fs.existsSync(reviewsDir)) return [];
  const members = fs.existsSync(membersDir)
    ? fs.readdirSync(membersDir).filter((f) => f.endsWith(".json"))
        .map((f) => ({ ...JSON.parse(fs.readFileSync(path.join(membersDir, f), "utf8")), slug: f.slice(0, -5) }))
    : [];
  const memberBySlug = new Map(members.map((m) => [m.slug, m]));

  return fs
    .readdirSync(reviewsDir)
    .filter((f) => f.endsWith(".md"))
    .map((f) => {
      const { data, content } = matter(fs.readFileSync(path.join(reviewsDir, f), "utf8"));
      if (!isPublicReview(data)) return null;
      const m = memberBySlug.get(data.member);
      const reviewers = m ? [{ name: m.name, slug: m.isCurrent ? m.slug : null }] : [];
      const { dnf: _privateDnf, ...publicData } = data;
      return { ...publicData, review: content.trim() || null, reviewers };
    })
    .filter(Boolean)
    .sort((a, b) => (a.createdAt || "").localeCompare(b.createdAt || ""));
}

module.exports = function () {
  return loadReviewsFrom(DATA);
};
module.exports.isPublicReview = isPublicReview;
module.exports.loadReviewsFrom = loadReviewsFrom;
