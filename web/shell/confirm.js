/* The confirmation dialog, shared.
 *
 * Three callers now: launching a run, rejecting a match, and deleting the
 * rejected matches. Each writes its own title, lede and body, then hands over
 * the action to run if the operator confirms. What they share is the part
 * that must not vary -- nothing pre-selected, the destructive action visually
 * distinct, one place that knows how to close.
 *
 * It lived inside runs.js while only runs used it. Left there, the Rejected
 * page would have had to reach across into another module's private state,
 * which is how two dialogs that behave slightly differently get born.
 */
(function () {
"use strict";

  const { showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let ACTION = null;

  function close() {
    $("confirm").hidden = true;
    document.body.style.overflow = "";
    ACTION = null;
  }

  /* Called AFTER the caller has written the body, so the button state a
     caller set -- disabled behind an acknowledgement checkbox, say -- is
     preserved rather than reset from under it. */
  function open(action) {
    ACTION = action;
    $("confirm").hidden = false;
    document.body.style.overflow = "hidden";
  }

  /* Arm the button after the dialog is already up.
   *
   * A caller whose confirmation depends on a slow query opens the dialog
   * first with a spinner -- a click that waits silently on an unchanged
   * button reads as "nothing happened" and invites a second one -- then
   * calls this when it knows what confirming should do. */
  function arm(action) {
    ACTION = action;
  }

  $("confirm-cancel").addEventListener("click", close);
  $("confirm").addEventListener("click", (e) => {
    if (e.target === $("confirm")) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("confirm").hidden) close();
  });

  $("confirm-go").addEventListener("click", async () => {
    const action = ACTION;
    if (!action) return;
    const label = $("confirm-go").textContent;
    $("confirm-go").disabled = true;
    $("confirm-go").textContent = "Working…";
    try {
      await action();
      close();
    } catch (e) {
      /* Closed either way: the error belongs on the page behind the dialog,
         where it stays visible, not stranded under a modal the user then has
         to dismiss to read it. */
      showError($("errors"), e.message);
      close();
    } finally {
      $("confirm-go").textContent = label;
    }
  });

  window.ConfirmDialog = { open, arm, close };
})();
