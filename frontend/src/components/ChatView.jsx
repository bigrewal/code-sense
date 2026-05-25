import React, { useEffect, useMemo, useRef } from 'react';
import { Bot } from 'lucide-react';
import ChatMessage from './ChatMessage';
import ChatInput from './ChatInput';
import ProgressTimelineMessage from './ProgressTimelineMessage';

export default function ChatView({
  messages,
  onSendMessage,
  isStreaming,
  selectedRepo,
}) {
  const messagesEndRef = useRef(null);
  const displayItems = useMemo(() => {
    const items = [];
    let index = 0;

    while (index < messages.length) {
      const message = messages[index];

      if (message.message_type === 'progress_event') {
        const progressMessages = [];
        const startIndex = index;

        while (index < messages.length && messages[index].message_type === 'progress_event') {
          progressMessages.push(messages[index]);
          index += 1;
        }

        const assistantMessage =
          index < messages.length &&
          messages[index].message_type === 'chat_message' &&
          messages[index].role === 'assistant'
            ? messages[index]
            : null;

        items.push({
          type: 'progress_group',
          key: progressMessages[0]?.client_id || `progress-${progressMessages[0]?.created_at || startIndex}`,
          messages: progressMessages,
          message: assistantMessage,
        });

        if (assistantMessage) {
          index += 1;
        }

        continue;
      }

      items.push({
        type: 'chat_message',
        key: message.client_id || `${message.created_at || 'message'}-${index}`,
        message,
      });

      index += 1;
    }

    return items;
  }, [messages]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="border-b border-slate-200/80 bg-white/70 px-6 py-4 backdrop-blur-md">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Conversation</h3>
        <p className="mt-1 text-sm text-slate-700">
          {selectedRepo ? `Repo: ${selectedRepo}` : 'No repository selected'}
        </p>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-5 sm:px-8">
        {messages.length === 0 ? (
          <div className="mx-auto mt-16 max-w-lg rounded-2xl border border-dashed border-slate-300 bg-white/60 p-10 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-cyan-50 text-cyan-700">
              <Bot size={22} />
            </div>
            <p className="text-base font-semibold text-slate-800">Start a focused repo conversation</p>
            <p className="mt-2 text-sm text-slate-500">
              Ask architecture, setup, or implementation questions about the selected repository.
            </p>
          </div>
        ) : (
          <>
            {displayItems.map((item) => (
              item.type === 'progress_group' ? (
                <ProgressTimelineMessage key={item.key} messages={item.messages} message={item.message} />
              ) : (
                <ChatMessage key={item.key} message={item.message} />
              )
            ))}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      <ChatInput onSend={onSendMessage} disabled={isStreaming} />
    </div>
  );
}
