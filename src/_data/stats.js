// Pre-compute statistics for the /stats/ page at build time.
// All heavy lifting happens here so the template just passes JSON to D3.

const buildBooks = require("./books.js");

module.exports = function () {
  const books = buildBooks();
  if (!books.length) return null;

  // Only books that have been read (have a meeting date)
  const read = books.filter((b) => b.meetingDate);

  // ── Books by year (year read) ────────────────────────────────────────
  const byYearMap = new Map();
  for (const b of read) {
    const y = b.year;
    if (!y) continue;
    if (!byYearMap.has(y)) byYearMap.set(y, { year: y, count: 0, pages: 0 });
    const entry = byYearMap.get(y);
    entry.count += 1;
    entry.pages += b.pageCount || 0;
  }
  const byYear = [...byYearMap.values()].sort((a, b) => a.year - b.year);

  // ── Topic distribution ───────────────────────────────────────────────
  const topicMap = new Map();
  for (const b of read) {
    const t = b.topic || "Uncategorized";
    topicMap.set(t, (topicMap.get(t) || 0) + 1);
  }
  const topics = [...topicMap.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count);

  // ── Publication year distribution ────────────────────────────────────
  const pubYears = read
    .filter((b) => b.publicationYear)
    .map((b) => b.publicationYear)
    .sort((a, b) => a - b);

  // Group into decades for histogram
  const decadeMap = new Map();
  for (const y of pubYears) {
    const decade = Math.floor(y / 10) * 10;
    decadeMap.set(decade, (decadeMap.get(decade) || 0) + 1);
  }
  const pubDecades = [...decadeMap.entries()]
    .map(([decade, count]) => ({ decade, count }))
    .sort((a, b) => a.decade - b.decade);

  // ── Fiction vs non-fiction ───────────────────────────────────────────
  const fictionCount = read.filter((b) => b.fiction).length;
  const nonfictionCount = read.length - fictionCount;

  // ── Picker leaderboard ──────────────────────────────────────────────
  const pickerMap = new Map();
  for (const b of read) {
    if (!b.pickerName) continue;
    if (!pickerMap.has(b.pickerName))
      pickerMap.set(b.pickerName, { name: b.pickerName, count: 0, pages: 0 });
    const entry = pickerMap.get(b.pickerName);
    entry.count += 1;
    entry.pages += b.pageCount || 0;
  }
  const pickers = [...pickerMap.values()].sort((a, b) => b.count - a.count);

  // ── Page count stats ────────────────────────────────────────────────
  const pageCounts = read
    .filter((b) => b.pageCount)
    .map((b) => ({ title: b.title, pages: b.pageCount, slug: b.slug }))
    .sort((a, b) => b.pages - a.pages);

  const totalPages = pageCounts.reduce((s, b) => s + b.pages, 0);
  const avgPages = pageCounts.length
    ? Math.round(totalPages / pageCounts.length)
    : 0;

  // ── Fun facts ───────────────────────────────────────────────────────
  const oldest = read
    .filter((b) => b.publicationYear)
    .sort((a, b) => a.publicationYear - b.publicationYear)[0];
  const newest = read
    .filter((b) => b.publicationYear)
    .sort((a, b) => b.publicationYear - a.publicationYear)[0];
  const longest = pageCounts[0] || null;
  const shortest = pageCounts[pageCounts.length - 1] || null;

  // ── Years active ────────────────────────────────────────────────────
  const firstYear = byYear.length ? byYear[0].year : null;
  const lastYear = byYear.length ? byYear[byYear.length - 1].year : null;
  const yearsActive = firstYear && lastYear ? lastYear - firstYear + 1 : 0;

  return {
    totalBooks: read.length,
    totalPages,
    avgPages,
    yearsActive,
    firstYear,
    lastYear,
    fictionCount,
    nonfictionCount,
    byYear,
    topics,
    pubDecades,
    pubYears,
    pickers,
    longest,
    shortest,
    oldest,
    newest,
  };
};
