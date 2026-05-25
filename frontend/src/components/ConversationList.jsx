import React from 'react';
import { MessageSquare, Plus, Trash2 } from 'lucide-react';

export default function ConversationList({
  conversations,
  selectedConversationId,
  onSelectConversation,
  onNewConversation,
  onDeleteConversation
}) {
  const handleDelete = (e, conversationId) => {
    e.stopPropagation();
    if (window.confirm('Are you sure you want to delete this conversation?')) {
      onDeleteConversation(conversationId);
    }
  };

  return (
    <div className="mt-4">
      <div className="mb-2 flex items-center justify-between px-1">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <MessageSquare size={16} />
          <span>Conversations</span>
        </div>
        <button
          onClick={onNewConversation}
          className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 transition hover:bg-slate-100"
          title="New conversation"
        >
          <Plus size={16} />
        </button>
      </div>

      <div className="space-y-1">
        {conversations.length === 0 ? (
          <div className="rounded-lg border border-dashed border-slate-300 px-3 py-3 text-xs italic text-slate-500">
            No conversations yet
          </div>
        ) : (
          conversations.map((conv) => (
            <div
              key={conv.conversation_id}
              onClick={() => onSelectConversation(conv.conversation_id)}
              className={`group flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm transition ${
                selectedConversationId === conv.conversation_id
                  ? 'bg-cyan-50 text-cyan-800 ring-1 ring-cyan-200'
                  : 'bg-white text-slate-600 hover:bg-slate-100'
              }`}
            >
              <MessageSquare size={14} className="flex-shrink-0" />
              <span className="flex-1 truncate">
                {conv.title || `Chat ${new Date(conv.created_at).toLocaleDateString()}`}
              </span>
              <button
                onClick={(e) => handleDelete(e, conv.conversation_id)}
                className="rounded p-1 text-red-600 opacity-0 transition group-hover:opacity-100 hover:bg-red-50"
                title="Delete conversation"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
