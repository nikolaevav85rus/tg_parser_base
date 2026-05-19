// Shared sidebar TOC active-section highlighting
(function () {
  "use strict";
  const links = document.querySelectorAll(".sidebar-nav .nav-link");
  if (links.length === 0) return;

  const sections = Array.from(links)
    .map((l) => {
      const href = l.getAttribute("href");
      return href && href.startsWith("#") ? document.querySelector(href) : null;
    });

  function onScroll() {
    const y = window.scrollY + 100;
    let activeIdx = 0;
    sections.forEach((s, i) => {
      if (s && s.offsetTop <= y) activeIdx = i;
    });
    links.forEach((l, i) => l.classList.toggle("active", i === activeIdx));
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();
