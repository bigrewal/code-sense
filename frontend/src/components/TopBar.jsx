import React from 'react';
import { Search, FolderSearch } from 'lucide-react';

export default function TopBar({ repoCount, searchTerm, onSearchChange }) {
  return (
    <div className="surface-card fade-in-up rounded-3xl px-4 py-3 sm:px-5 sm:py-4">
      <div className="flex flex-col gap-4">
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500">
            Repository Workspace
          </div>
          <div className="mt-1.5 flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <h1 className="text-[2rem] font-semibold leading-none tracking-[-0.04em] sm:text-[2.9rem]">
                <span className="text-slate-900">Code</span>
                <span className="ml-1 text-slate-600">Sense</span>
              </h1>
            </div>
          </div>
        </div>

        <div className="grid items-stretch gap-3 lg:grid-cols-[minmax(0,1.7fr)_minmax(200px,0.45fr)]">
          <label className="relative block">
            <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-4 text-slate-400">
              <Search size={16} />
            </div>
            <input
              type="text"
              placeholder="Search repositories by name..."
              value={searchTerm}
              onChange={(e) => onSearchChange(e.target.value)}
              className="h-full min-h-[60px] w-full rounded-2xl border border-slate-200 bg-white py-3 pl-11 pr-4 text-sm text-slate-700 outline-none transition placeholder:text-slate-400 focus:border-slate-400 focus:ring-2 focus:ring-slate-200"
            />
          </label>

          <div className="flex min-h-[60px] flex-col justify-center rounded-2xl border border-slate-200 bg-white px-4 py-2.5">
            <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
              <FolderSearch size={14} />
              Catalog
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-xl font-semibold text-slate-900">{repoCount}</span>
              <span className="text-sm text-slate-500">repositories</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
