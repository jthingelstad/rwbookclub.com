// Machine-readable "what does this build think the next book is" marker.
//
// Consumed by Oliver's self-healing publish check (agent/commands.ensure_site_reflects_next_book):
// if the deployed marker disagrees with the live corpus — a book was added but its deferred
// publish was lost, or a meeting rolled over so the next book changed — Oliver republishes
// automatically. The goal is that no person has to do anything on meeting rollover.
//
// nextBookSlug is the earliest still-upcoming book (same rule the journey/homepage use), derived
// from each meeting's local date+time (see website/lib/clock via the books _data).

module.exports = class {
  data() {
    return { permalink: "/next.json", eleventyExcludeFromCollections: true };
  }

  render({ books }) {
    const upcoming = (books || [])
      .filter((b) => b.isUpcoming && b.meetingDate)
      .sort((a, b) => (a.meetingDate || "").localeCompare(b.meetingDate || ""));
    const next = upcoming[0] || null;
    return JSON.stringify({
      nextBookSlug: next ? next.slug : null,
      nextMeetingDate: next ? String(next.meetingDate).slice(0, 10) : null,
      generatedAt: new Date().toISOString(),
    });
  }
};
