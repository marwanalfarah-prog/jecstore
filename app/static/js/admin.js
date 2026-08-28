/* Admin panel behaviour (Part I §17.6, Part II §5).
 *
 * Three things, all of which staff previously had to supply themselves:
 *
 *   1. Confirmation before an action that cannot be taken back. The panel is
 *      append-only — closing a money box or retiring a promocode writes
 *      history rather than deleting a row — so "undo" does not exist. A
 *      double-check before the write is the only protection there is.
 *
 *   2. Submit-once. Every write here is a plain form POST followed by a
 *      redirect. On a slow connection in a branch the page sits still, staff
 *      press again, and the second press posts a second payment or a second
 *      stock movement. The button disables itself on submit and says so.
 *
 *   3. Filter forms that submit themselves when a select changes, without the
 *      inline onchange handlers that were scattered through the templates.
 *
 * Progressive enhancement throughout: with this file blocked, every form still
 * works exactly as it did before — unconfirmed and double-submittable, but
 * never broken.
 */
(function () {
  "use strict";

  /* --- 1. Confirm --------------------------------------------------------
     `data-confirm="…"` on a form or a button. On a button it wins over the
     form's, so "Cancel order" can ask a sharper question than the panel it
     sits in. */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    var submitter = event.submitter;
    var question =
      (submitter && submitter.getAttribute("data-confirm")) ||
      form.getAttribute("data-confirm");

    if (question && !window.confirm(question)) {
      event.preventDefault();
      return;
    }

    markBusy(form, submitter);
  });

  /* --- 2. Submit-once ----------------------------------------------------
     Disabling the button would drop its name/value from the payload, and
     several screens POST `name="status" value="approved"` straight off the
     button. So the guard is `aria-disabled` plus a captured click handler —
     the value still submits on the first press and nothing submits on the
     second. */
  function markBusy(form, submitter) {
    if (form.hasAttribute("data-no-busy")) return;
    if (form.dataset.busy === "1") return;
    form.dataset.busy = "1";

    var buttons = form.querySelectorAll(
      'button[type="submit"], button:not([type]), input[type="submit"]'
    );
    Array.prototype.forEach.call(buttons, function (button) {
      button.setAttribute("aria-disabled", "true");
      button.classList.add("is-busy");
    });

    if (submitter && submitter.dataset.busyLabel) {
      submitter.textContent = submitter.dataset.busyLabel;
    }
  }

  document.addEventListener(
    "click",
    function (event) {
      var button = event.target.closest
        ? event.target.closest('[aria-disabled="true"]')
        : null;
      if (button && button.form) {
        event.preventDefault();
        event.stopPropagation();
      }
    },
    true
  );

  /* A form that fails validation never leaves the page, so the guard has to
     lift again or the screen is stuck showing a busy button it will never
     clear. */
  document.addEventListener("invalid", function (event) {
    var form = event.target && event.target.form;
    if (form) clearBusy(form);
  }, true);

  /* Back/forward restores a cached page with its buttons still disabled. */
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    Array.prototype.forEach.call(
      document.querySelectorAll('form[data-busy="1"]'),
      clearBusy
    );
  });

  function clearBusy(form) {
    delete form.dataset.busy;
    Array.prototype.forEach.call(
      form.querySelectorAll('[aria-disabled="true"]'),
      function (button) {
        button.removeAttribute("aria-disabled");
        button.classList.remove("is-busy");
      }
    );
  }

  /* --- 3. Auto-submitting filters ---------------------------------------- */
  document.addEventListener("change", function (event) {
    var control = event.target;
    if (!control || !control.matches || !control.matches("[data-autosubmit]")) return;
    if (control.form) control.form.requestSubmit();
  });
})();
