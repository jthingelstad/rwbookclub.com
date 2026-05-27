// Structured JSON export of the full archive, one row per book.
// Intended for LLMs / analysts doing cross-book queries.

module.exports = class {
  data() {
    return {
      permalink: "/books.json",
      eleventyExcludeFromCollections: true,
    };
  }

  render({ books, reviews, site }) {
    const reviewsFor = (bookId) =>
      (reviews || [])
        .filter((r) => Array.isArray(r.bookIds) && r.bookIds.includes(bookId))
        .sort((a, b) => (a.createdAt || "").localeCompare(b.createdAt || ""))
        .map((r) => ({
          reviewers: (r.reviewers || []).map((p) => ({
            name: p.name,
            slug: p.slug || null,
          })),
          rating: r.rating ?? null,
          dnf: !!r.dnf,
          wouldRecommend: !!r.wouldRecommend,
          discussionQuality: r.discussionQuality ?? null,
          review: r.review || null,
          favoriteQuote: r.favoriteQuote || null,
          createdAt: r.createdAt || null,
        }));

    const payload = {
      generated: new Date().toISOString(),
      site: site.url,
      count: books.length,
      books: books.map((b) => {
        const bookReviews = reviewsFor(b.id);
        const widths = Array.isArray(b.coverWidths) ? b.coverWidths : [];
        const coverBase = `${site.url}/assets/images/covers/${b.slug}`;
        return {
          slug: b.slug,
          url: `${site.url}/books/${b.slug}/`,
          textUrl: `${site.url}/books/${b.slug}.txt`,
          title: b.title,
          subtitle: b.subtitle || null,
          authors: b.authors || [],
          topic: b.topic || null,
          fiction: !!b.fiction,
          publicationYear: b.publicationYear || null,
          pageCount: b.pageCount || null,
          isbn13: b.isbn13 || null,
          olKey: b.olKey || null,
          olUrl: b.olKey ? `https://openlibrary.org${b.olKey}` : null,
          coverUrl: b.hasCover && widths.length ? `${coverBase}-${widths[widths.length - 1]}.jpg` : null,
          coverUrls: b.hasCover && widths.length
            ? Object.fromEntries(widths.map((w) => [w, `${coverBase}-${w}.jpg`]))
            : null,
          meetingDate: b.meetingDate || null,
          yearRead: b.year || null,
          pickers: (b.pickerNames || []).map((name, i) => ({
            name,
            slug: (b.pickerSlugs || [])[i] || null,
          })),
          placeholder: !!b.placeholder,
          synopsis: b.synopsis || null,
          reviewCount: bookReviews.length,
          reviews: bookReviews,
        };
      }),
    };

    return JSON.stringify(payload, null, 2);
  }
};
