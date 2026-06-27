(function () {
    function toggleAdvanced() {
        var el = document.getElementById('create-advanced');
        var btn = document.getElementById('adv-toggle');
        el.classList.toggle('open');
        btn.innerHTML = el.classList.contains('open')
            ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg> Advanced options'
            : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg> Advanced options';
    }
    window.toggleAdvanced = toggleAdvanced;

    var fileInput = document.getElementById('file-input');
    var urlInput = document.getElementById('source_url');
    var fileInfoBox = document.getElementById('file-info-box');
    var fileNameDisplay = document.getElementById('file-name-display');

    if (fileInput) {
        fileInput.addEventListener('change', function () {
            if (fileInput.files.length > 0) {
                fileNameDisplay.innerText = fileInput.files[0].name;
                fileInfoBox.style.display = 'flex';
                urlInput.value = '';
                urlInput.disabled = true;
                urlInput.style.opacity = '0.5';
            } else {
                clearFileInput();
            }
        });
    }

    function clearFileInput() {
        if (!fileInput) return;
        fileInput.value = '';
        fileInfoBox.style.display = 'none';
        urlInput.disabled = false;
        urlInput.style.opacity = '1';
    }
    window.clearFileInput = clearFileInput;

    var previewVideoId = null;
    var previewDuration = 0;

    function fmtTime(s) {
        s = Math.max(0, s);
        var m = Math.floor(s / 60);
        var sec = Math.floor(s % 60);
        if (m >= 60) {
            var h = Math.floor(m / 60);
            m = m % 60;
            return h + ':' + (m < 10 ? '0' : '') + m + ':' + (sec < 10 ? '0' : '') + sec;
        }
        return m + ':' + (sec < 10 ? '0' : '') + sec;
    }

    function fmtDuration(sec) {
        if (sec >= 3600) {
            var h = Math.floor(sec / 3600);
            sec = sec % 3600;
            var m = Math.floor(sec / 60);
            sec = Math.floor(sec % 60);
            return h + 'h ' + m + 'm ' + sec + 's';
        }
        var m = Math.floor(sec / 60);
        var s = Math.floor(sec % 60);
        return m + 'm ' + s + 's';
    }

    function resetPreview() {
        document.getElementById('preview-meta').style.display = 'none';
        document.getElementById('timeline-section').style.display = 'none';
        document.getElementById('preview-loading').classList.remove('show');
        var iframe = document.getElementById('yt-preview');
        iframe.style.display = 'none';
        iframe.src = '';
        document.getElementById('preview-placeholder').style.display = 'flex';
        previewVideoId = null;
        previewDuration = 0;
    }

    function syncTimeline() {
        var startS = document.getElementById('clip_start_slider');
        var endS = document.getElementById('clip_end_slider');
        var start = parseFloat(startS.value);
        var end = parseFloat(endS.value);
        var pctStart = previewDuration > 0 ? (start / previewDuration * 100) : 0;
        var pctEnd = previewDuration > 0 ? (end / previewDuration * 100) : 100;
        var pctRange = pctEnd - pctStart;
        document.getElementById('start-label').innerText = fmtTime(start);
        document.getElementById('end-label').innerText = fmtTime(end);
        document.getElementById('timeline-max-label').innerText = fmtTime(previewDuration);
        document.getElementById('thumb-start').style.left = pctStart + '%';
        document.getElementById('thumb-end').style.left = pctEnd + '%';
        document.getElementById('timeline-fill').style.left = pctStart + '%';
        document.getElementById('timeline-fill').style.width = Math.max(pctRange, 0.5) + '%';
        document.getElementById('range-label').innerText = (end - start).toFixed(0) + 's selected';
        if (start <= 0 && end >= previewDuration) {
            document.getElementById('clip_start').value = 0;
            document.getElementById('clip_end').value = 0;
        } else {
            document.getElementById('clip_start').value = start;
            document.getElementById('clip_end').value = end;
        }
        if (previewVideoId) {
            var iframe = document.getElementById('yt-preview');
            iframe.src = 'https://www.youtube.com/embed/' + previewVideoId + '?start=' + Math.floor(start) + '&end=' + Math.ceil(end) + '&autoplay=0&rel=0';
        }
    }

    function onStartSlide() {
        var startS = document.getElementById('clip_start_slider');
        var endS = document.getElementById('clip_end_slider');
        if (parseFloat(startS.value) >= parseFloat(endS.value)) {
            startS.value = Math.max(0, parseFloat(endS.value) - 1);
        }
        syncTimeline();
    }
    window.onStartSlide = onStartSlide;

    function onEndSlide() {
        var startS = document.getElementById('clip_start_slider');
        var endS = document.getElementById('clip_end_slider');
        if (parseFloat(endS.value) <= parseFloat(startS.value)) {
            endS.value = Math.min(previewDuration, parseFloat(startS.value) + 1);
        }
        syncTimeline();
    }
    window.onEndSlide = onEndSlide;

    function previewVideo() {
        var url = urlInput.value.trim();
        if (!url) { showToast('Enter a video URL first', true); return; }
        var btn = document.getElementById('preview-btn');
        var loading = document.getElementById('preview-loading');
        btn.disabled = true;
        btn.innerText = 'Loading…';
        loading.classList.add('show');
        resetPreview();
        var formData = new FormData();
        formData.append('source_url', url);
        fetch('/preview', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + window.API_TOKEN },
            body: formData
        }).then(function (r) {
            if (!r.ok) throw new Error('Preview failed');
            return r.json();
        }).then(function (data) {
            loading.classList.remove('show');
            previewDuration = data.duration;
            previewVideoId = data.video_id || null;
            document.getElementById('preview-title').innerText = data.title;
            document.getElementById('preview-duration').innerText = 'Duration: ' + fmtDuration(data.duration);
            document.getElementById('preview-meta').style.display = 'block';
            var startS = document.getElementById('clip_start_slider');
            var endS = document.getElementById('clip_end_slider');
            startS.max = data.duration;
            endS.max = data.duration;
            startS.value = 0;
            endS.value = data.duration;
            document.getElementById('timeline-section').style.display = 'block';
            if (previewVideoId) {
                document.getElementById('preview-placeholder').style.display = 'none';
                document.getElementById('yt-preview').style.display = 'block';
            }
            var resSel = document.getElementById('download_resolution');
            var currentVal = resSel.value;
            resSel.innerHTML = '';
            var avail = data.available_resolutions || [];
            if (avail.length) {
                var bestOpt = document.createElement('option');
                bestOpt.value = 'best';
                bestOpt.textContent = 'Best';
                resSel.appendChild(bestOpt);
                avail.forEach(function (r) {
                    var opt = document.createElement('option');
                    opt.value = r.value;
                    opt.textContent = r.label;
                    resSel.appendChild(opt);
                });
                resSel.value = avail.some(function (r) { return r.value === currentVal; }) ? currentVal : 'best';
            } else {
                ['best', '2160p', '1440p', '1080p', '720p', '480p', '360p'].forEach(function (v) {
                    var opt = document.createElement('option');
                    opt.value = v;
                    opt.textContent = v;
                    if (v === '1080p') opt.selected = true;
                    resSel.appendChild(opt);
                });
            }
            syncTimeline();
            btn.disabled = false;
            btn.innerText = 'Preview';
        }).catch(function (e) {
            loading.classList.remove('show');
            btn.disabled = false;
            btn.innerText = 'Preview';
            showToast('Error: ' + e.message, true);
        });
    }
    window.previewVideo = previewVideo;

    function handleFormSuccess(event) {
        try {
            var xhr = event.detail.xhr;
            if (xhr.status >= 200 && xhr.status < 300) {
                var jobId = null;
                try {
                    var body = JSON.parse(xhr.responseText);
                    jobId = body.job_id;
                } catch (e) {}
                showToast('Video enqueued');
                if (jobId) {
                    window.location.href = '/app/jobs/' + jobId;
                    return;
                }
                window.location.href = '/app';
            } else {
                var msg = 'Failed to enqueue job.';
                try {
                    var errBody = JSON.parse(xhr.responseText);
                    msg = typeof errBody.detail === 'string' ? errBody.detail : (errBody.detail ? errBody.detail[0]?.msg || msg : msg);
                } catch (e) {}
                showToast('Error: ' + msg, true);
            }
        } catch (e) {
            showToast('Something went wrong', true);
        }
    }
    window.handleFormSuccess = handleFormSuccess;
})();
