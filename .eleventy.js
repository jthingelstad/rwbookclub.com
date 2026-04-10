module.exports = function (eleventyConfig) {
  // Pass static assets through unchanged
  eleventyConfig.addPassthroughCopy({ "src/assets": "assets" });
  eleventyConfig.addPassthroughCopy({ "src/CNAME": "CNAME" });

  // Watch CSS so --serve picks up edits
  eleventyConfig.addWatchTarget("src/assets/css/");

  // ── Filters ────────────────────────────────────────────────────────────
  eleventyConfig.addFilter("monthYear", (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", {
      month: "long",
      year: "numeric",
      timeZone: "UTC",
    });
  });

  eleventyConfig.addFilter("longDate", (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    });
  });

  eleventyConfig.addFilter("year", (iso) => {
    if (!iso) return "";
    return new Date(iso).getUTCFullYear();
  });

  // Join author names: ["A", "B", "C"] → "A, B, and C"
  eleventyConfig.addFilter("joinNames", (list) => {
    if (!Array.isArray(list) || list.length === 0) return "";
    if (list.length === 1) return list[0];
    if (list.length === 2) return `${list[0]} and ${list[1]}`;
    return `${list.slice(0, -1).join(", ")}, and ${list[list.length - 1]}`;
  });

  // Reviews for a given book id (Nunjucks lacks selectattr)
  eleventyConfig.addFilter("reviewsForBook", (reviews, bookId) => {
    if (!Array.isArray(reviews)) return [];
    return reviews
      .filter((r) => Array.isArray(r.bookIds) && r.bookIds.includes(bookId))
      .sort((a, b) => (a.createdAt || "").localeCompare(b.createdAt || ""));
  });

  // Awards for a given book id
  eleventyConfig.addFilter("awardsForBook", (awards, bookId) => {
    if (!Array.isArray(awards)) return [];
    return awards.filter(
      (a) => Array.isArray(a.books) && a.books.some((b) => b.id === bookId)
    );
  });

  // Reviews authored by a given member id
  eleventyConfig.addFilter("reviewsByMember", (reviews, memberId) => {
    if (!Array.isArray(reviews)) return [];
    return reviews.filter(
      (r) => Array.isArray(r.memberIds) && r.memberIds.includes(memberId)
    );
  });

  // Books a member has not yet reviewed (any kind of review counts, including
  // DNF). `books` is already sorted most-recent-first by the fetch script.
  eleventyConfig.addFilter("unreviewedFor", (books, memberId, reviews) => {
    if (!Array.isArray(books)) return [];
    const reviewed = new Set();
    if (Array.isArray(reviews)) {
      for (const r of reviews) {
        if (!Array.isArray(r.memberIds) || !r.memberIds.includes(memberId)) continue;
        for (const bid of r.bookIds || []) reviewed.add(bid);
      }
    }
    return books.filter((b) => b.meetingDate && !reviewed.has(b.id));
  });

  // RFC-822 / RFC-2822 date for the RSS feed
  eleventyConfig.addFilter("rfc822Date", (iso) => {
    if (!iso) return "";
    return new Date(iso).toUTCString();
  });

  // Take the first n items of an array (Nunjucks `slice` does something else)
  eleventyConfig.addFilter("limit", (arr, n) => {
    if (!Array.isArray(arr)) return [];
    return arr.slice(0, n);
  });

  return {
    dir: {
      input: "src",
      includes: "_includes",
      data: "_data",
      output: "_site",
    },
    templateFormats: ["njk", "md", "html"],
    htmlTemplateEngine: "njk",
    markdownTemplateEngine: "njk",
  };
};
