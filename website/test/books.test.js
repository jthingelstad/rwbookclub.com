const assert = require("node:assert/strict");
const test = require("node:test");

const { attachRelated, cleanSubjects } = require("../src/_data/books");

test("subject cleaning removes machine noise and case-insensitive duplicates", () => {
  assert.deepEqual(
    cleanSubjects(["Ecology", "ecology", "nyt:list=2026", "New York Times Bestseller"]),
    [{ key: "ecology", label: "Ecology" }]
  );
});

test("related books prioritize a shared author and retain a readable reason", () => {
  const books = [
    { slug: "a", title: "Forest One", authors: ["A. Writer"], subjects: ["Trees"], topic: "Nature", fiction: false, year: 2020 },
    { slug: "b", title: "Forest Two", authors: ["A. Writer"], subjects: ["Trees"], topic: "Nature", fiction: false, year: 2021 },
    { slug: "c", title: "City Systems", authors: ["B. Writer"], subjects: ["Cities"], topic: "Nature", fiction: false, year: 2022 }
  ];
  attachRelated(books);
  assert.equal(books[0].related[0].slug, "b");
  assert.equal(books[0].related[0].reason, "By the same author");
});
