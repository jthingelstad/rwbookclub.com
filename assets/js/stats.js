// Chart.js rendering for the /stats/ page.
// Data is embedded at build time in a <script type="application/json"> block.

(function () {
  var colors = {
    accent: "#9C4A26",
    accentInk: "#7A3A1E",
    inkSoft: "#5C5247",
    inkFaint: "#9B8E7C",
    bgAlt: "#F4EFE3",
    rule: "rgba(230, 220, 204, 0.5)",
    bg: "#FBF8F3",
  };

  var treemapPalette = [
    "#9C4A26", "#B8623A", "#7A3A1E", "#C4835A", "#5C5247",
    "#8B7355", "#6B5B4E", "#A67C5B", "#D4A574", "#3E3428",
    "#C9956B", "#AB7D5A",
  ];

  var font = { family: '"Fraunces", Georgia, serif' };

  var defaultScales = {
    x: {
      ticks: { color: colors.inkFaint, font: font },
      grid: { color: colors.rule },
    },
    y: {
      ticks: { color: colors.inkFaint, font: font },
      grid: { color: colors.rule },
    },
  };

  var data = JSON.parse(document.getElementById("chart-data").textContent);

  // ── Books per year ────────────────────────────────────────────────

  new Chart(document.getElementById("chart-books-by-year"), {
    type: "bar",
    data: {
      labels: data.byYear.map(function (d) { return d.year; }),
      datasets: [{
        label: "Books",
        data: data.byYear.map(function (d) { return d.count; }),
        backgroundColor: colors.accent,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              return ctx.parsed.y + (ctx.parsed.y === 1 ? " book" : " books");
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: colors.inkFaint, font: font },
          grid: { display: false },
        },
        y: {
          beginAtZero: true,
          ticks: { color: colors.inkFaint, font: font, stepSize: 2 },
          grid: { color: colors.rule },
        },
      },
    },
  });

  // ── Pages per year ────────────────────────────────────────────────

  new Chart(document.getElementById("chart-pages-by-year"), {
    type: "bar",
    data: {
      labels: data.byYear.map(function (d) { return d.year; }),
      datasets: [{
        label: "Pages",
        data: data.byYear.map(function (d) { return d.pages; }),
        backgroundColor: colors.inkSoft,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              return ctx.parsed.y.toLocaleString("en-US") + " pages";
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: colors.inkFaint, font: font },
          grid: { display: false },
        },
        y: {
          beginAtZero: true,
          ticks: {
            color: colors.inkFaint,
            font: font,
            callback: function (v) { return v.toLocaleString("en-US"); },
          },
          grid: { color: colors.rule },
        },
      },
    },
  });

  // ── Topic treemap ─────────────────────────────────────────────────

  new Chart(document.getElementById("chart-topics"), {
    type: "treemap",
    data: {
      datasets: [{
        tree: data.topics,
        key: "count",
        labels: {
          display: true,
          formatter: function (ctx) {
            if (ctx.type !== "data") return "";
            var d = ctx.raw._data;
            return [d.name, d.count + (d.count === 1 ? " book" : " books")];
          },
          color: colors.bg,
          font: [
            { size: 13, weight: "500", family: font.family },
            { size: 11, family: font.family },
          ],
          overflow: "hidden",
          padding: 4,
        },
        backgroundColor: function (ctx) {
          if (ctx.type !== "data") return colors.bgAlt;
          return treemapPalette[ctx.dataIndex % treemapPalette.length];
        },
        borderWidth: 2,
        borderColor: colors.bg,
        spacing: 1,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: function (items) {
              if (!items.length) return "";
              return items[0].raw._data.name;
            },
            label: function (item) {
              var d = item.raw._data;
              return d.count + (d.count === 1 ? " book" : " books");
            },
          },
        },
      },
    },
  });

  // ── Publication decade histogram ──────────────────────────────────

  new Chart(document.getElementById("chart-pub-decade"), {
    type: "bar",
    data: {
      labels: data.pubDecades.map(function (d) { return d.decade + "s"; }),
      datasets: [{
        label: "Books",
        data: data.pubDecades.map(function (d) { return d.count; }),
        backgroundColor: colors.accent,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              return ctx.parsed.y + (ctx.parsed.y === 1 ? " book" : " books");
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: colors.inkFaint, font: font },
          grid: { display: false },
        },
        y: {
          beginAtZero: true,
          ticks: { color: colors.inkFaint, font: font, stepSize: 10 },
          grid: { color: colors.rule },
        },
      },
    },
  });

  // ── Pickers horizontal bar chart ──────────────────────────────────

  new Chart(document.getElementById("chart-pickers"), {
    type: "bar",
    data: {
      labels: data.pickers.map(function (d) { return d.name; }),
      datasets: [{
        label: "Books picked",
        data: data.pickers.map(function (d) { return d.count; }),
        backgroundColor: colors.accent,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: "y",
      plugins: {
        legend: { display: false },
      },
      scales: {
        x: {
          beginAtZero: true,
          ticks: { color: colors.inkFaint, font: font, stepSize: 5 },
          grid: { color: colors.rule },
        },
        y: {
          ticks: { color: colors.inkSoft, font: font, autoSkip: false },
          grid: { display: false },
        },
      },
    },
  });
})();
