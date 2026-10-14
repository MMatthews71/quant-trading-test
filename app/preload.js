'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  getConfig: () => ipcRenderer.invoke('app:config'),
  getSeries: (args) => ipcRenderer.invoke('db:series', args),
  reloadDb: () => ipcRenderer.invoke('db:reload'),
  onDbChanged: (cb) => ipcRenderer.on('db:changed', () => cb()),

  listRows: (table) => ipcRenderer.invoke('tables:list', table),
  addRow: (table, row) => ipcRenderer.invoke('tables:add', table, row),
  updateRow: (table, id, row) => ipcRenderer.invoke('tables:update', table, id, row),
  deleteRow: (table, id) => ipcRenderer.invoke('tables:delete', table, id),

  loadParams: () => ipcRenderer.invoke('params:load'),
  saveParams: (patch) => ipcRenderer.invoke('params:save', patch),

  loadBacktest: () => ipcRenderer.invoke('backtest:load'),
  runBacktest: () => ipcRenderer.invoke('backtest:run'),
  exportBacktestCsv: () => ipcRenderer.invoke('backtest:exportCsv'),
  onBacktestChanged: (cb) => ipcRenderer.on('backtest:changed', () => cb()),

  loadLiveParams: () => ipcRenderer.invoke('live:loadParams'),
  saveLiveParams: (patch) => ipcRenderer.invoke('live:saveParams', patch),
  loadLive: () => ipcRenderer.invoke('live:load'),
  exportLiveCsv: () => ipcRenderer.invoke('live:exportCsv'),
  onLiveChanged: (cb) => ipcRenderer.on('live:changed', () => cb()),

  startUpdater: () => ipcRenderer.invoke('updater:start'),
  stopUpdater: () => ipcRenderer.invoke('updater:stop'),
  getUpdaterStatus: () => ipcRenderer.invoke('updater:status'),

  onUpdaterLog: (cb) => ipcRenderer.on('updater:log', (_e, data) => cb(data)),
  onUpdaterStatus: (cb) => ipcRenderer.on('updater:status', (_e, data) => cb(data)),
});
