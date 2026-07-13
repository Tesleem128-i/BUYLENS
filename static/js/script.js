/* ===================== PRISM — interaction layer ===================== */

gsap.registerPlugin(ScrollTrigger);

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------------------------------------------------------------
   0. Intro: lens calibration boot sequence (plays once on load)
--------------------------------------------------------------- */
(function introSequence(){
  const intro = document.getElementById('intro');
  if(!intro) return;

  const finish = () => {
    intro.remove();
    document.documentElement.style.overflow = '';
  };

  document.documentElement.style.overflow = 'hidden';

  if(reduceMotion){
    setTimeout(finish, 200);
    return;
  }

  const scrambleEl = document.getElementById('intro-scramble');
  const statusEl = document.getElementById('intro-status');
  const barEl = document.getElementById('intro-bar');
  const finalText = 'PRISM';
  const glyphs = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%◆✦❖▲◇✧0123456789';
  const totalFrames = 22;
  let frame = 0;

  function scrambleTick(){
    frame++;
    let out = '';
    for(let i = 0; i < finalText.length; i++){
      const revealAt = ((i + 1) / finalText.length) * totalFrames;
      out += frame >= revealAt ? finalText[i] : glyphs[Math.floor(Math.random() * glyphs.length)];
    }
    if(scrambleEl) scrambleEl.textContent = out;
    if(frame < totalFrames) setTimeout(scrambleTick, 30);
  }
  scrambleTick();

  const statuses = ['ALIGNING PRISM', 'REFRACTING MARKET SIGNAL', 'SPLITTING PRICE SPECTRUM', 'FOCUSED'];
  let statusIdx = 0;
  const statusInterval = setInterval(() => {
    statusIdx++;
    if(statusIdx < statuses.length && statusEl) statusEl.textContent = statuses[statusIdx];
    else clearInterval(statusInterval);
  }, 460);

  requestAnimationFrame(() => {
    if(barEl){
      barEl.style.transition = 'width 1.85s cubic-bezier(.4,0,.2,1)';
      barEl.style.width = '100%';
    }
  });

  setTimeout(() => {
    intro.classList.add('is-opening');
    setTimeout(finish, 1300);
  }, 1950);
})();

/* ---------------------------------------------------------------
   1. Ambient particle starfield (fixed background canvas)
--------------------------------------------------------------- */
(function starfield(){
  const canvas = document.getElementById('bg-canvas');
  const ctx = canvas.getContext('2d');
  let w, h, particles;

  function resize(){
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
  function makeParticles(){
    const count = Math.min(140, Math.floor((w*h)/12000));
    particles = Array.from({length:count}, () => ({
      x: Math.random()*w,
      y: Math.random()*h,
      r: Math.random()*1.6 + 0.3,
      vx: (Math.random()-0.5)*0.15,
      vy: (Math.random()-0.5)*0.15,
      hue: Math.random() > 0.5 ? '76,224,255' : '157,107,255',
      a: Math.random()*0.6 + 0.15
    }));
  }
  function tick(){
    ctx.clearRect(0,0,w,h);
    ctx.fillStyle = '#05070C';
    ctx.fillRect(0,0,w,h);
    for(const p of particles){
      p.x += p.vx; p.y += p.vy;
      if(p.x < 0) p.x = w; if(p.x > w) p.x = 0;
      if(p.y < 0) p.y = h; if(p.y > h) p.y = 0;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI*2);
      ctx.fillStyle = `rgba(${p.hue},${p.a})`;
      ctx.shadowColor = `rgba(${p.hue},0.8)`;
      ctx.shadowBlur = 6;
      ctx.fill();
    }
    if(!reduceMotion) requestAnimationFrame(tick);
  }
  resize(); makeParticles(); tick();
  window.addEventListener('resize', () => { resize(); makeParticles(); });
})();

/* ---------------------------------------------------------------
   2. Scroll progress bar
--------------------------------------------------------------- */
(function progress(){
  const bar = document.getElementById('progress-bar');
  window.addEventListener('scroll', () => {
    const h = document.documentElement;
    const pct = (h.scrollTop) / (h.scrollHeight - h.clientHeight) * 100;
    bar.style.width = pct + '%';
  });
})();

/* ---------------------------------------------------------------
   3. Void scene: eye opens -> logo reveals -> fades as user scrolls
--------------------------------------------------------------- */
gsap.timeline({
  scrollTrigger:{ trigger:'#void', start:'top top', end:'+=90%', scrub:0.6, pin:true }
})
.to('.void__eye', { scale:1.6, opacity:0, duration:1, ease:'power1.in' })
.to('.void__reveal', { opacity:1, y:0, duration:0.8 }, '-=0.6')
.to('.void__reveal', { opacity:0, y:-30, duration:0.6 }, '+=0.3')
.to('.scroll-cue', { opacity:0, duration:0.3 }, 0.1);

/* ---------------------------------------------------------------
   4. Generic reveal-on-scroll for section headers / cards
--------------------------------------------------------------- */
const revealTargets = [
  '.city__label', '.city .scene-title', '.city .scene-sub', '.city-block',
  '.ask__orb-wrap', '.ask .scene-title', '.ask__form', '.ask__chips',
  '.scanner .eyebrow', '.scanner .scene-title', '.scanner-phone', '.scanner-features li',
  '.trust .eyebrow', '.trust .scene-title', '.glass-panel',
  '.stat'
];
revealTargets.forEach(sel => {
  document.querySelectorAll(sel).forEach((el, i) => {
    el.classList.add('reveal');
    ScrollTrigger.create({
      trigger: el,
      start: 'top 88%',
      onEnter: () => setTimeout(() => el.classList.add('is-visible'), i * 40),
      once: true
    });
  });
});

/* stagger city blocks a bit more visibly */
gsap.utils.toArray('.city-block').forEach((el, i) => {
  gsap.fromTo(el, { opacity:0, y:50 }, {
    opacity:1, y:0, duration:0.7, delay:i*0.05, ease:'power2.out',
    scrollTrigger:{ trigger:'#city-grid', start:'top 85%' }
  });
});

/* ---------------------------------------------------------------
   5. City block click -> highlight category (feeds "ask" narrative)
--------------------------------------------------------------- */
document.querySelectorAll('.city-block').forEach(block => {
  block.addEventListener('click', () => {
    document.querySelectorAll('.city-block').forEach(b => b.style.borderColor = '');
    block.style.borderColor = 'rgba(76,224,255,.6)';
    const input = document.getElementById('ask-input');
    if(input) input.placeholder = `Try: best ${block.dataset.cat.toLowerCase()} deal right now`;
    document.getElementById('ask')?.scrollIntoView({ behavior:'smooth' });
  });
});

/* ---------------------------------------------------------------
   6. Ask orb: prompt form + quick chips (simulated AI response)
--------------------------------------------------------------- */
(function askOrb(){
  const form = document.getElementById('ask-form');
  const input = document.getElementById('ask-input');
  const response = document.getElementById('ask-response');
  const orb = document.getElementById('ask-orb');
  const chips = document.querySelectorAll('.chip');

  const responses = {
    default: (q) => `Scanning the market for "${q}" — comparing stores, reviews, and authenticity signals…`,
  };

  function analyze(q){
    if(!q) return;
    orb.style.animation = 'none';
    orb.offsetHeight; /* reflow */
    orb.style.animation = 'orbFloat 1.2s ease-in-out infinite';
    response.textContent = '';
    const msg = responses.default(q);
    let i = 0;
    const type = () => {
      if(i <= msg.length){
        response.textContent = msg.slice(0, i);
        i += 2;
        setTimeout(type, 12);
      } else {
        setTimeout(() => { orb.style.animation = 'orbFloat 5s ease-in-out infinite'; }, 600);
      }
    };
    type();
  }

  form?.addEventListener('submit', (e) => {
    e.preventDefault();
    analyze(input.value.trim());
  });
  chips.forEach(chip => {
    chip.addEventListener('click', () => {
      input.value = chip.dataset.val;
      analyze(chip.dataset.val);
    });
  });
})();

/* ---------------------------------------------------------------
   7. Story: pinned horizontal-scroll cinematic track
--------------------------------------------------------------- */
(function storyTrack(){
  const track = document.querySelector('.story__track');
  const panels = gsap.utils.toArray('.story__panel');
  const dotsWrap = document.getElementById('story-dots');
  const isMobile = window.matchMedia('(max-width: 900px)').matches;

  if(isMobile){
    /* Mobile: no horizontal pin — panels are normal vertical sections.
       Just reveal each panel's content as it scrolls into view. */
    panels.forEach(panel => {
      gsap.fromTo(panel.querySelectorAll('.eyebrow, h3, p, .story__scatter, .price-orbit, .review-merge, .scan-box, .timeline, .rec-grid, .panel-image'),
        { opacity:0, y:24 },
        {
          opacity:1, y:0, duration:0.6, stagger:0.08, ease:'power2.out',
          scrollTrigger:{ trigger: panel, start:'top 82%', toggleActions:'play none none reverse' }
        });
    });
    return;
  }

  panels.forEach((_, i) => {
    const dot = document.createElement('span');
    if(i === 0) dot.classList.add('active');
    dotsWrap.appendChild(dot);
  });
  const dots = dotsWrap.querySelectorAll('span');

  const scrollTween = gsap.to(track, {
    x: () => -(track.scrollWidth - window.innerWidth),
    ease: 'none',
    scrollTrigger:{
      trigger: '#story',
      start: 'top top',
      end: () => '+=' + (track.scrollWidth - window.innerWidth),
      scrub: 0.6,
      pin: true,
      invalidateOnRefresh: true,
      onUpdate: (self) => {
        const idx = Math.round(self.progress * (panels.length - 1));
        dots.forEach((d, i) => d.classList.toggle('active', i === idx));
      }
    }
  });

  /* panel-local reveals keyed to horizontal scroll position */
  panels.forEach(panel => {
    gsap.fromTo(panel.querySelectorAll('.eyebrow, h3, p, .story__scatter, .price-orbit, .review-merge, .scan-box, .timeline, .rec-grid, .panel-image'),
      { opacity:0, y:30 },
      {
        opacity:1, y:0, duration:0.6, stagger:0.08, ease:'power2.out',
        scrollTrigger:{
          trigger: panel,
          containerAnimation: scrollTween,
          start: 'left 70%',
          toggleActions: 'play none none reverse'
        }
      });
  });
})();

/* ---------------------------------------------------------------
   8. Stats counter
--------------------------------------------------------------- */
document.querySelectorAll('.stat__num[data-count]').forEach(el => {
  const target = parseFloat(el.dataset.count);
  const decimals = (el.dataset.count.split('.')[1] || '').length;
  ScrollTrigger.create({
    trigger: el,
    start: 'top 85%',
    once: true,
    onEnter: () => {
      gsap.fromTo({ v: 0 }, { v: target }, {
        v: target, duration: 1.6, ease:'power2.out',
        onUpdate: function(){ el.textContent = this.targets()[0].v.toFixed(decimals); }
      });
    }
  });
});

/* ---------------------------------------------------------------
   9. Final scene: converging particles forming the wordmark
--------------------------------------------------------------- */
(function finalCanvas(){
  const canvas = document.getElementById('final-canvas');
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  let w, h, points, raf;

  function resize(){
    const rect = canvas.parentElement.getBoundingClientRect();
    w = canvas.width = rect.width;
    h = canvas.height = rect.height;
  }
  function buildPoints(){
    const count = 90;
    points = Array.from({length:count}, () => ({
      x: Math.random()*w, y: Math.random()*h,
      tx: w/2 + (Math.random()-0.5)*260,
      ty: h/2 + (Math.random()-0.5)*90,
      hue: Math.random() > 0.5 ? '76,224,255' : '157,107,255'
    }));
  }
  let progress = 0;
  function draw(){
    ctx.clearRect(0,0,w,h);
    progress = Math.min(1, progress + 0.006);
    points.forEach(p => {
      const x = p.x + (p.tx - p.x) * progress;
      const y = p.y + (p.ty - p.y) * progress;
      ctx.beginPath();
      ctx.arc(x, y, 1.6, 0, Math.PI*2);
      ctx.fillStyle = `rgba(${p.hue},0.7)`;
      ctx.shadowColor = `rgba(${p.hue},0.9)`;
      ctx.shadowBlur = 8;
      ctx.fill();
    });
    if(progress < 1 && !reduceMotion) raf = requestAnimationFrame(draw);
  }
  ScrollTrigger.create({
    trigger:'#begin', start:'top 70%', once:true,
    onEnter: () => { resize(); buildPoints(); progress = 0; draw(); }
  });
  window.addEventListener('resize', resize);
})();