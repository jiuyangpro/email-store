(function () {
  function getCookie(name) {
    const cookieValue = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith(name + "="));
    return cookieValue ? decodeURIComponent(cookieValue.split("=")[1]) : "";
  }

  async function sendCode(button) {
    const emailField = document.getElementById(button.dataset.emailField);
    const statusEl = document.getElementById(button.dataset.statusId);
    const sendUrl = button.dataset.sendUrl;
    const purpose = button.dataset.purpose;

    if (!emailField || !sendUrl || !purpose) {
        return;
    }

    const email = emailField.value.trim();
    if (!email) {
      if (statusEl) statusEl.textContent = "请先填写邮箱地址。";
      return;
    }

    button.disabled = true;
    if (statusEl) statusEl.textContent = "验证码发送中，请稍候...";

    try {
      const body = new URLSearchParams();
      body.set("email", email);
      body.set("purpose", purpose);

      const response = await fetch(sendUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: body.toString(),
      });

      const data = await response.json();
      if (statusEl) {
        statusEl.textContent = data.message || (data.ok ? "验证码已发送。" : "验证码发送失败。");
      }
    } catch (error) {
      if (statusEl) statusEl.textContent = "验证码发送失败，请稍后再试。";
    } finally {
      button.disabled = false;
    }
  }

  document.querySelectorAll(".send-email-code-btn").forEach((button) => {
    button.addEventListener("click", () => sendCode(button));
  });
})();
