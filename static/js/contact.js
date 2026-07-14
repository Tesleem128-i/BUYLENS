/* ===================== PRISM — contact form handler =====================
   Shared by index.html, terms.html, and privacy.html. Finds every
   [data-contact-form] on the page and wires it up to POST /api/contact,
   which forwards the message to the Prism inbox via Brevo.
========================================================================= */
(function () {
  const forms = document.querySelectorAll('[data-contact-form]');
  if (!forms.length) return;

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

  forms.forEach((form) => {
    const statusEl = form.querySelector('[data-contact-status]');
    const submitBtn = form.querySelector('button[type="submit"]');
    const submitLabel = submitBtn ? submitBtn.querySelector('.contact-form__submit-label') : null;
    const nameInput = form.querySelector('input[name="name"]');
    const emailInput = form.querySelector('input[name="email"]');
    const messageInput = form.querySelector('textarea[name="message"]');

    function setStatus(text, type) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.className = 'contact-form__status is-visible contact-form__status--' + type;
    }

    function setLoading(isLoading) {
      if (submitBtn) submitBtn.disabled = isLoading;
      if (submitLabel) submitLabel.textContent = isLoading ? 'Sending…' : 'Send message';
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();

      const name = (nameInput?.value || '').trim();
      const email = (emailInput?.value || '').trim();
      const message = (messageInput?.value || '').trim();

      if (!name || !email || !message) {
        setStatus('Fill in your name, email, and message.', 'error');
        return;
      }

      setLoading(true);
      setStatus('Sending…', 'info');

      try {
        const res = await fetch('/api/contact', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
          },
          body: JSON.stringify({ name, email, message }),
        });
        const data = await res.json().catch(() => ({}));

        if (res.ok && data.success) {
          setStatus("Message sent — we'll get back to you soon.", 'success');
          form.reset();
        } else {
          setStatus(data.error || 'Something went wrong. Please try again.', 'error');
        }
      } catch (err) {
        setStatus("Couldn't reach the server — check your connection and try again.", 'error');
      } finally {
        setLoading(false);
      }
    });
  });
})();