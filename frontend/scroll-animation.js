/**
 * scroll-animation.js
 * Section reveal animations + nav active-state highlighting via GSAP ScrollTrigger.
 * (Three.js camera triggers removed — using CSS background instead.)
 */

const ScrollAnimation = (() => {

  let isSetup = false;

  function init() {
    if (isSetup) return;
    isSetup = true;

    if (!window.gsap || !window.ScrollTrigger) {
      _fallbackScrollObserver();
      return;
    }

    gsap.registerPlugin(ScrollTrigger);
    _setupSectionReveal();
    _setupNavHighlight();
    _setupParallaxHero();
  }

  /* ── Section reveal ──────────────────────────────────── */
  function _setupSectionReveal() {
    // Hero is always visible
    document.querySelector('.section-hero')?.classList.add('in-view');

    document.querySelectorAll('.section:not(.section-hero)').forEach(section => {
      ScrollTrigger.create({
        trigger: section,
        start: 'top 78%',
        onEnter:     () => section.classList.add('in-view'),
        onLeaveBack: () => section.classList.remove('in-view'),
      });
    });
  }

  /* ── Nav active state ────────────────────────────────── */
  function _setupNavHighlight() {
    const navLinks = document.querySelectorAll('.nav-link');

    document.querySelectorAll('.section[data-section]').forEach(section => {
      ScrollTrigger.create({
        trigger: section,
        start: 'top 50%',
        end: 'bottom 50%',
        onToggle: ({ isActive }) => {
          if (!isActive) return;
          navLinks.forEach(l => l.classList.remove('active'));
          const link = document.querySelector(`.nav-link[data-section="${section.dataset.section}"]`);
          if (link) link.classList.add('active');
        },
      });
    });
  }

  /* ── Hero parallax — disabled (caused auto-scroll jank) */
  function _setupParallaxHero() {
    // intentionally empty
  }

  /* ── Fallback (no GSAP) ──────────────────────────────── */
  function _fallbackScrollObserver() {
    document.querySelector('.section-hero')?.classList.add('in-view');

    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) entry.target.classList.add('in-view');
        else entry.target.classList.remove('in-view');
      });
    }, { threshold: 0.15 });

    document.querySelectorAll('.section').forEach(s => observer.observe(s));
  }

  /* ── Public ──────────────────────────────────────────── */
  function scrollToSection(index) {
    const target = document.querySelector(`.section[data-section="${index}"]`);
    if (target) target.scrollIntoView({ behavior: 'smooth' });
  }

  return { init, scrollToSection };

})();
