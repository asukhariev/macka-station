const { app, BrowserWindow, Tray, shell, nativeImage, screen } = require('electron');
const AutoLaunch = require('electron-auto-launch');
const path = require('path');

app.dock.hide();

let tray = null;
let win = null;

const autoLauncher = new AutoLaunch({ name: 'Mackafunk' });
autoLauncher.enable();

function createWindow() {
  win = new BrowserWindow({
    width: 400,
    height: 560,
    show: false,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    backgroundColor: '#03070f',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  win.loadURL('https://macka.agtc.app');

  win.on('blur', () => win.hide());

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith('https://macka.agtc.app')) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });
}

function toggleWindow() {
  if (win.isVisible()) {
    win.hide();
    return;
  }

  const trayBounds = tray.getBounds();
  const display = screen.getDisplayNearestPoint({ x: trayBounds.x, y: trayBounds.y });
  const winBounds = win.getBounds();

  // position below tray icon, horizontally centered on it
  let x = Math.round(trayBounds.x + trayBounds.width / 2 - winBounds.width / 2);
  let y = Math.round(trayBounds.y + trayBounds.height + 4);

  // keep inside screen
  x = Math.max(display.workArea.x, Math.min(x, display.workArea.x + display.workArea.width - winBounds.width));

  win.setPosition(x, y, false);
  win.show();
  win.focus();
}

app.whenReady().then(() => {
  const icon = nativeImage.createFromPath(path.join(__dirname, 'trayTemplate.png'));
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip('Mackafunk');
  tray.on('click', toggleWindow);

  createWindow();
});

app.on('window-all-closed', () => {});
