// Sitemap <lastmod> for every page: the date its content last really changed, as
// near as the club record can say. The record keeps no edit times, so a page
// takes the newest dated fact it shows, never the build time (Google ignores
// lastmod on a site where it is always "now"):
//
// - a book: its meetings that have happened and the public reviews of it
//   (review createdAt, as a Central date);
// - an author or a list: when the club met on its books (those pages show the
//   books, not their reviews); a member: the meetings they hosted (and so picked)
//   that have happened, their public reviews, their lists and the day they
//   joined;
// - a section page (/books/, /authors/, /members/, /lists/): the newest of its
//   entries; /meetings/: the newest meeting that has happened; /, /stats/: the
//   newest fact on the site; /about/ and /oliver/ (prose plus club counts): the
//   newer of that and their template's last commit.
//
// A book with no dated fact (scheduled but not met yet, or on no meeting at all)
// takes the newest meeting that has happened: books are picked and scheduled at
// or after the club's last meeting. Every date is a W3C date, 'YYYY-MM-DD' in the
// club's timezone, and never later than today.

const clock = require("./clock");

const maxDate = (dates) => dates.filter(Boolean).reduce((a, b) => (a === null || b > a ? b : a), null);

// A review's createdAt (UTC instant) as the club's local date.
function centralDate(iso) {
  if (!iso) return null;
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? null : clock.centralToday(at);
}

function pageDates({ books, meetings, reviews, lists, members, authors, templateDates = {}, now = new Date() }) {
  const today = clock.centralToday(now);
  const cap = (date) => (date && date > today ? today : date);
  const held = meetings.filter((m) => m.date && !clock.isUpcoming(m.date, m.startTime, now));
  const lastMeeting = maxDate(held.map((m) => m.date));

  const meetingDatesByBook = new Map();
  for (const m of held) {
    for (const slug of m.books || []) {
      if (!meetingDatesByBook.has(slug)) meetingDatesByBook.set(slug, []);
      meetingDatesByBook.get(slug).push(m.date);
    }
  }
  const reviewDate = (r) => cap(centralDate(r.createdAt));

  const metDate = (slug) => maxDate(meetingDatesByBook.get(slug) || []);
  const bookDate = new Map();
  for (const b of books) {
    const own = maxDate([metDate(b.slug), ...reviews.filter((r) => r.book === b.slug).map(reviewDate)]);
    bookDate.set(b.slug, own || lastMeeting);
  }

  const authorDate = new Map(
    authors.map((a) => [
      a.slug,
      maxDate(books.filter((b) => (b.authors || []).includes(a.name)).map((b) => metDate(b.slug))) || lastMeeting,
    ])
  );

  const listDate = new Map(
    lists.map((l) => [l.slug, maxDate((l.books || []).map((e) => metDate(e.book))) || lastMeeting])
  );

  const memberDate = new Map(
    members.map((m) => [
      m.slug,
      maxDate([
        cap(m.joined || null),
        ...held.filter((mt) => (mt.host || []).includes(m.slug)).map((mt) => mt.date),
        ...reviews.filter((r) => r.member === m.slug).map(reviewDate),
        ...lists.filter((l) => l.owner === m.slug).map((l) => listDate.get(l.slug)),
      ]) || lastMeeting,
    ])
  );

  const currentMembers = members.filter((m) => m.isCurrent);
  const site = maxDate([
    lastMeeting,
    ...bookDate.values(),
    ...reviews.map(reviewDate),
    ...currentMembers.map((m) => cap(m.joined || null)),
  ]);

  const dates = new Map([
    ["/", site],
    ["/about/", maxDate([site, cap(templateDates.about)])],
    ["/stats/", site],
    ["/books/", maxDate([...bookDate.values()])],
    ["/members/", maxDate(currentMembers.map((m) => memberDate.get(m.slug)))],
    ["/lists/", maxDate([...listDate.values()])],
    ["/authors/", maxDate([...authorDate.values()])],
    ["/meetings/", lastMeeting],
    ["/oliver/", maxDate([site, cap(templateDates.oliver)])],
  ]);
  for (const [slug, date] of bookDate) dates.set(`/books/${slug}/`, date);
  for (const m of currentMembers) dates.set(`/members/${m.slug}/`, memberDate.get(m.slug));
  for (const [slug, date] of listDate) dates.set(`/lists/${slug}/`, date);
  for (const [slug, date] of authorDate) dates.set(`/authors/${slug}/`, date);
  return dates;
}

module.exports = { centralDate, pageDates };
