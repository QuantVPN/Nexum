// Small progressive enhancements; the pages work without JavaScript.
document.addEventListener("click", function (event) {
  var el = event.target.closest("[data-print]");
  if (el) { event.preventDefault(); window.print(); }
});
document.addEventListener("submit", function (event) {
  var form = event.target;
  var message = form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) { event.preventDefault(); }
});
document.addEventListener("change", function (event) {
  var el = event.target;
  if (el.hasAttribute("data-autosubmit") && el.form) { el.form.submit(); }
});
