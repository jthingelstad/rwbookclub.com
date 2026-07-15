// Reviews are canonical corpus data: one Markdown file per review with slug-based
// frontmatter (book + member slugs) and the prose body. Reviewer name/slug is
// derived from the member here so templates keep rendering names. Joined to books
// and members by slug (see the reviewsForBook / reviewsByMember filters).

const fs = require("fs");
const path = require("path");
const matter = require("gray-matter");
const corpus = require("../../lib/corpus");

const DATA = corpus.dataDir();
const REVIEWS_DIR = path.join(DATA, "reviews");
const MEMBERS_DIR = path.join(DATA, "members");

module.exports = function () {
  if (!fs.existsSync(REVIEWS_DIR)) return [];
  const members = fs.existsSync(MEMBERS_DIR)
    ? fs.readdirSync(MEMBERS_DIR).filter((f) => f.endsWith(".json"))
        .map((f) => ({ ...JSON.parse(fs.readFileSync(path.join(MEMBERS_DIR, f), "utf8")), slug: f.slice(0, -5) }))
    : [];
  const memberBySlug = new Map(members.map((m) => [m.slug, m]));

  return fs
    .readdirSync(REVIEWS_DIR)
    .filter((f) => f.endsWith(".md"))
    .map((f) => {
      const { data, content } = matter(fs.readFileSync(path.join(REVIEWS_DIR, f), "utf8"));
      const m = memberBySlug.get(data.member);
      const reviewers = m ? [{ name: m.name, slug: m.isCurrent ? m.slug : null }] : [];
      return { ...data, review: content.trim() || null, reviewers };
    })
    .sort((a, b) => (a.createdAt || "").localeCompare(b.createdAt || ""));
};
