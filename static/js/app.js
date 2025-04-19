// static/js/app.js

// --- Configuration ---
const UPLOAD_URL = '/upload';
const ALLOWED_EXTENSIONS = ['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac'];

// --- DOM Elements ---
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const uploadButton = document.getElementById('upload-button');
const languageSelect = document.getElementById('language');
const fileList = document.getElementById('file-list');
const noFilesMessage = document.getElementById('no-files-message');
const messagesDiv = document.getElementById('messages');

// --- State ---
let filesToUpload = []; // Array to hold File objects

// --- Socket.IO Connection ---
// Connect to the Socket.IO server (URL should match Flask server)
const socket = io(); // Assumes server is on the same host/port

socket.on('connect', () => {
    console.log('Socket.IO connected!', socket.id);
    addMessage('Connected to transcription service.', 'info');
});

socket.on('disconnect', () => {
    console.log('Socket.IO disconnected.');
    addMessage('Disconnected from transcription service.', 'warning');
});

socket.on('status', (data) => {
    console.log('Status message received:', data);
    addMessage(data.message || 'Received status update.', 'info');
});

socket.on('progress_update', (data) => {
    console.log('Progress update:', data);
    updateFileStatus(data.job_id, `Progress: ${data.progress}% - ${data.message}`);
});

socket.on('transcription_complete', (data) => {
    console.log('Transcription complete:', data);
    const statusText = `Complete! <a href="${data.download_url}" target="_blank" download>Download (${data.output_file})</a>`;
    updateFileStatus(data.job_id, statusText, 'success');
});

socket.on('transcription_error', (data) => {
    console.error('Transcription error:', data);
    updateFileStatus(data.job_id, `Error: ${data.message}`, 'error');
});


// --- Helper Functions ---
function addMessage(message, type = 'info') {
    const msgElement = document.createElement('div');
    msgElement.className = `message message-${type}`; // e.g., message-info, message-error
    msgElement.textContent = message;
    messagesDiv.appendChild(msgElement);
    // Auto-scroll or limit messages if needed
}

function clearMessages() {
    messagesDiv.innerHTML = '';
}

function isFileTypeAllowed(filename) {
    const extension = filename.split('.').pop().toLowerCase();
    return ALLOWED_EXTENSIONS.includes(extension);
}

function updateFileListUI() {
    fileList.innerHTML = ''; // Clear existing list
    if (filesToUpload.length === 0) {
        fileList.appendChild(noFilesMessage);
        noFilesMessage.style.display = 'block';
        uploadButton.disabled = true;
    } else {
        noFilesMessage.style.display = 'none';
        filesToUpload.forEach((file, index) => {
            const listItem = document.createElement('li');
            listItem.id = `file-item-${index}`; // Assign an ID for potential future updates
            listItem.innerHTML = `
                <span class="filename">${escapeHTML(file.name)}</span>
                <span class="filesize">(${(file.size / 1024 / 1024).toFixed(2)} MB)</span>
                <span class="status" id="status-${index}">Pending</span>
                <button class="remove-btn" data-index="${index}">Remove</button>
            `;
            fileList.appendChild(listItem);
        });
        uploadButton.disabled = false;

        // Add event listeners to remove buttons
        document.querySelectorAll('.remove-btn').forEach(button => {
            button.addEventListener('click', handleRemoveFile);
        });
    }
}

// Function to find file list item by job_id (requires associating job_id with UI elements)
// This is tricky because job_id is assigned *after* upload starts.
// We'll map job_id to filename temporarily after upload response.
let jobIdToFileMap = {}; // { 'job_id_1': 'filename.mp3', 'job_id_2': 'another.wav' }

function findListItemByJobId(jobId) {
    const filename = jobIdToFileMap[jobId];
    if (!filename) return null;

    // Find the list item by filename text content (less robust but works for now)
    const fileItems = fileList.querySelectorAll('li');
    for (const item of fileItems) {
        const nameSpan = item.querySelector('.filename');
        if (nameSpan && nameSpan.textContent === filename) {
            return item;
        }
    }
    return null; // Not found
}


function updateFileStatus(jobId, statusMessage, statusClass = '') {
     // Find the corresponding list item using the jobId -> filename mapping
    const listItem = findListItemByJobId(jobId);
    if (listItem) {
        const statusSpan = listItem.querySelector('.status');
        if (statusSpan) {
            statusSpan.innerHTML = statusMessage; // Use innerHTML to allow links
            statusSpan.className = `status ${statusClass}`; // Add error/success class
        }
    } else {
        console.warn(`Could not find list item for job_id: ${jobId}`);
        // Optionally add a general message if item isn't found
        addMessage(`Update for job ${jobId}: ${statusMessage}`, statusClass || 'info');
    }
}


function handleFiles(droppedFiles) {
    clearMessages(); // Clear previous messages
    let newlyAddedFiles = [];
    for (const file of droppedFiles) {
        if (isFileTypeAllowed(file.name)) {
             // Avoid adding duplicates by name (simple check)
            if (!filesToUpload.some(existingFile => existingFile.name === file.name)) {
                 newlyAddedFiles.push(file);
                 addMessage(`Added file: ${file.name}`, 'info');
            } else {
                 addMessage(`Skipped duplicate file: ${file.name}`, 'warning');
            }
        } else {
            addMessage(`Disallowed file type: ${file.name}`, 'error');
        }
    }
    filesToUpload = filesToUpload.concat(newlyAddedFiles);
    updateFileListUI();
}

function handleRemoveFile(event) {
    const indexToRemove = parseInt(event.target.dataset.index, 10);
    const removedFile = filesToUpload.splice(indexToRemove, 1);
    if (removedFile.length > 0) {
         addMessage(`Removed file: ${removedFile[0].name}`, 'info');
    }
    updateFileListUI(); // Re-render the list
}

// Basic HTML escaping
function escapeHTML(str) {
    return str.replace(/[&<>"']/g, function (match) {
        return {
            '&': '&',
            '<': '<',
            '>': '>',
            '"': '"',
            "'": '''
        }[match];
    });
}


// --- Event Listeners ---

// Click on drop zone triggers file input
dropZone.addEventListener('click', () => {
    fileInput.click();
});

// File input change
fileInput.addEventListener('change', (event) => {
    handleFiles(event.target.files);
    // Reset file input to allow selecting the same file again
    fileInput.value = null;
});

// Drag and Drop events
dropZone.addEventListener('dragover', (event) => {
    event.preventDefault(); // Prevent default behavior (opening file)
    dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (event) => {
    event.preventDefault(); // Prevent default behavior
    dropZone.classList.remove('dragover');
    handleFiles(event.dataTransfer.files);
});

// Upload Button Click
uploadButton.addEventListener('click', async () => {
    if (filesToUpload.length === 0) {
        addMessage('No files selected to upload.', 'warning');
        return;
    }

    clearMessages();
    addMessage('Starting upload...', 'info');
    uploadButton.disabled = true; // Disable button during upload

    const formData = new FormData();
    filesToUpload.forEach(file => {
        formData.append('audio_files', file, file.name); // Key must match Flask's request.files.getlist
    });
    formData.append('language', languageSelect.value); // Add selected language

    // Reset mapping for new upload batch
    jobIdToFileMap = {};

    try {
        const response = await fetch(UPLOAD_URL, {
            method: 'POST',
            body: formData,
            // No 'Content-Type' header needed for FormData; browser sets it correctly
        });

        const result = await response.json();

        if (response.ok && response.status === 202) { // 202 Accepted
            addMessage('Upload successful. Transcription queued.', 'success');
            console.log("Queued jobs:", result.queued_jobs);
            // Map job IDs to filenames from the response for status updates
            result.queued_jobs.forEach(job => {
                jobIdToFileMap[job.job_id] = job.filename;
                 // Update initial status in UI
                 updateFileStatus(job.job_id, 'Queued for processing...');
            });
            // Handle any errors reported for specific files during queuing
            if (result.errors && result.errors.length > 0) {
                 result.errors.forEach(errMsg => addMessage(errMsg, 'error'));
            }
            // Clear the list of files ready for upload *after* successful queuing
             filesToUpload = [];
            // updateFileListUI(); // Keep the list showing the queued items

        } else {
            console.error("Upload failed:", result);
            const errorMsg = result.error || `Upload failed with status ${response.status}.`;
            addMessage(errorMsg, 'error');
             // Re-enable button on failure to allow retry
            uploadButton.disabled = false;
        }
    } catch (error) {
        console.error('Error during upload fetch:', error);
        addMessage(`Network error or server unreachable: ${error.message}`, 'error');
        // Re-enable button on fetch failure
        uploadButton.disabled = false;
    } finally {
         // Optionally re-enable button even on success if needed,
         // but usually better to wait until processing finishes or explicitly clear
         // uploadButton.disabled = false;
         console.log("Job ID to Filename Map:", jobIdToFileMap);
    }
});

// --- Initial UI Setup ---
updateFileListUI(); // Show initial message or empty list