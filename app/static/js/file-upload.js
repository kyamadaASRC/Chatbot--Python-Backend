// JavaScript for file upload, display, and delete functionality

document.addEventListener('DOMContentLoaded', function () {
    const fileInput = document.getElementById('file-upload-input');
    const fileListContainer = document.getElementById('uploaded-files-list');
    const sidebar = document.getElementById('sidebar');
    window.uploadedFiles = [];

    // Helper: Extract text from PDF using PDF.js
    async function extractPdfText(arrayBuffer) {
        if (!window.pdfjsLib) {
            alert('PDF.js library not loaded.');
            return '';
        }
        const pdf = await window.pdfjsLib.getDocument({data: arrayBuffer}).promise;
        let text = '';
        for (let i = 1; i <= pdf.numPages; i++) {
            const page = await pdf.getPage(i);
            const content = await page.getTextContent();
            text += content.items.map(item => item.str).join(' ') + '\n';
        }
        return text;
    }

    // Handle file upload
    fileInput.addEventListener('change', function (event) {
        const files = Array.from(event.target.files);
        files.forEach(file => {
            if (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) {
                const reader = new FileReader();
                reader.onload = async function(e) {
                    const arrayBuffer = e.target.result;
                    const pdfText = await extractPdfText(arrayBuffer);
                    window.uploadedFiles.push({
                        name: file.name,
                        content: pdfText
                    });
                    addFileToList(file);
                };
                reader.readAsArrayBuffer(file);
            } else {
                const reader = new FileReader();
                reader.onload = function(e) {
                    window.uploadedFiles.push({
                        name: file.name,
                        content: e.target.result
                    });
                    addFileToList(file);
                };
                reader.readAsText(file);
            }
        });
        fileInput.value = '';
        if (files.length > 0 && sidebar.classList.contains('collapsed')) {
            sidebar.classList.remove('collapsed');
            sidebar.classList.add('expanded');
        }
    });

    function addFileToList(file) {
        const fileItem = document.createElement('div');
        fileItem.className = 'uploaded-file-item';
        fileItem.innerHTML = `
            <span class="file-name">${file.name}</span>
            <button class="delete-file-btn" title="Delete">&times;</button>
        `;
        fileListContainer.appendChild(fileItem);

        fileItem.querySelector('.delete-file-btn').addEventListener('click', function () {
            fileListContainer.removeChild(fileItem);
            window.uploadedFiles = window.uploadedFiles.filter(f => f.name !== file.name);
            if (fileListContainer.children.length === 0) {
                sidebar.classList.remove('expanded');
                sidebar.classList.add('collapsed');
            }
        });
    }

    // Patch sendPrompt to include only extracted text for model, not in user textarea
    const origSendPrompt = window.sendPrompt;
    window.sendPrompt = function(userPrompt) {
        let modelPrompt = userPrompt;
        if (window.uploadedFiles.length > 0) {
            const context = window.uploadedFiles.map(f => `File: ${f.name}\n${f.content}`).join('\n\n');
            modelPrompt = `${userPrompt}\n\nContext from uploaded file(s):\n${context}`;
        }
        origSendPrompt(modelPrompt);
    };
});
