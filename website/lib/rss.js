// RFC-822 / RFC-2822 dates for the RSS feed.
//
// A meeting date is a Central calendar day with no time. At midnight UTC it reads as the evening
// before in Central (2026-07-28 became Jul 27, 7 pm CDT), so a bare date is stamped at noon UTC,
// which keeps its calendar day from UTC-11 to UTC+11. A value that carries a time is kept as is.

function rfc822Date(iso) {
  if (!iso) return "";
  const value = String(iso);
  return new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00Z` : value).toUTCString();
}

module.exports = { rfc822Date };
