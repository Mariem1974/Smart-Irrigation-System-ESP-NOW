function togglePassword() {
  const input = document.getElementById("passwordInput");
  const btn   = document.getElementById("toggleBtn");
  if (input.type === "password") {
    input.type      = "text";
    btn.textContent = "Hide";
  } else {
    input.type      = "password";
    btn.textContent = "Show";
  }
}

document.getElementById("loginForm")?.addEventListener("submit", function () {
  const btn = document.getElementById("loginBtn");
  btn.classList.add("loading");
  btn.querySelector(".btn-text").textContent = "Signing in...";
  btn.querySelector(".btn-arrow").textContent = "";
});