(function () {
  async function normalizeAndHashImage(file) {
    const imageUrl = URL.createObjectURL(file);
    try {
      const img = new Image();
      img.src = imageUrl;
      await img.decode();

      const maxSize = 900;
      const scale = Math.min(maxSize / img.width, maxSize / img.height, 1);
      const width = Math.max(1, Math.round(img.width * scale));
      const height = Math.max(1, Math.round(img.height * scale));

      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, width, height);

      const imageData = ctx.getImageData(0, 0, width, height).data;
      let hasAlpha = false;
      for (let i = 3; i < imageData.length; i += 4) {
        if (imageData[i] !== 255) {
          hasAlpha = true;
          break;
        }
      }

      const blob = await new Promise(function (resolve) {
        canvas.toBlob(
          resolve,
          hasAlpha ? "image/webp" : "image/jpeg",
          0.82
        );
      });
      const buffer = await blob.arrayBuffer();
      const digest = await crypto.subtle.digest("SHA-256", buffer);
      return Array.from(new Uint8Array(digest))
        .map(function (b) {
          return b.toString(16).padStart(2, "0");
        })
        .join("");
    } finally {
      URL.revokeObjectURL(imageUrl);
    }
  }

  function parseHashes(raw) {
    if (!raw) {
      return [];
    }
    try {
      const hashes = JSON.parse(raw);
      return Array.isArray(hashes) ? hashes : [];
    } catch (e) {
      return [];
    }
  }

  async function handleDuplicateGuard(input) {
    if (!input.files || !input.files.length) {
      return;
    }
    const existingHashes = parseHashes(input.dataset.existingHashes);
    if (!existingHashes.length) {
      return;
    }
    const allowFieldName = input.dataset.allowField;
    const allowField = allowFieldName
      ? document.querySelector('input[name="' + allowFieldName + '"]')
      : null;

    try {
      const hash = await normalizeAndHashImage(input.files[0]);
      if (existingHashes.indexOf(hash) === -1) {
        if (allowField) {
          allowField.value = "0";
        }
        return;
      }
      const confirmed = window.confirm("检测到这张图片已上传过，是否继续上传覆盖？");
      if (confirmed) {
        if (allowField) {
          allowField.value = "1";
        }
      } else {
        input.value = "";
        if (allowField) {
          allowField.value = "0";
        }
      }
    } catch (e) {
      if (allowField) {
        allowField.value = "0";
      }
    }
  }

  document.addEventListener("change", function (event) {
    const input = event.target;
    if (!input || input.type !== "file" || input.dataset.duplicateGuard !== "true") {
      return;
    }
    handleDuplicateGuard(input);
  });
})();
