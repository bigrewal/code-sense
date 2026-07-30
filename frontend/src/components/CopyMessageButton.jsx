import React, { useEffect, useState } from 'react';
import { Check, Copy } from 'lucide-react';

async function copyToClipboard(content) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(content);
      return;
    } catch {
      // Fall back for browsers that block clipboard access despite exposing the API.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = content;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();

  let copied = false;
  try {
    copied = document.execCommand('copy');
  } finally {
    document.body.removeChild(textarea);
  }

  if (!copied) {
    throw new Error('Unable to copy message');
  }
}

export default function CopyMessageButton({ content, inverted = false }) {
  const [status, setStatus] = useState('idle');

  useEffect(() => {
    if (status === 'idle') {
      return undefined;
    }

    const timeoutId = window.setTimeout(() => setStatus('idle'), 2000);
    return () => window.clearTimeout(timeoutId);
  }, [status]);

  const handleCopy = async () => {
    try {
      await copyToClipboard(content);
      setStatus('copied');
    } catch {
      setStatus('failed');
    }
  };

  const isCopied = status === 'copied';
  const label = isCopied ? 'Copied' : status === 'failed' ? 'Copy failed' : 'Copy';
  const Icon = isCopied ? Check : Copy;

  return (
    <button
      type="button"
      onClick={handleCopy}
      disabled={!content}
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] font-medium transition focus:outline-none focus:ring-2 focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50 ${
        inverted
          ? 'text-cyan-100 hover:bg-white/10 hover:text-white focus:ring-white/70 focus:ring-offset-cyan-600'
          : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700 focus:ring-cyan-500 focus:ring-offset-white'
      }`}
      aria-label={label === 'Copy' ? 'Copy message' : label}
      title={label === 'Copy' ? 'Copy message' : label}
    >
      <Icon size={12} aria-hidden="true" />
      <span aria-live="polite">{label}</span>
    </button>
  );
}
