const loginPanel = document.getElementById('login-panel') as HTMLElement
const panelShell = document.getElementById('panel-shell') as HTMLElement
const loginForm = document.getElementById('login-form') as HTMLFormElement
const loginError = document.getElementById('login-error') as HTMLElement

function showPanel(): void {
  loginForm.reset()
  loginError.textContent = ''
  loginPanel.hidden = true
  panelShell.hidden = false
}

function showLogin(message = ''): void {
  panelShell.hidden = true
  loginPanel.hidden = false
  loginError.textContent = message
}

async function checkSession(): Promise<void> {
  const response = await fetch('/streams', { credentials: 'same-origin' })
  if (response.ok) showPanel()
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  loginError.textContent = ''
  const formData = new FormData(loginForm)
  const response = await fetch('/auth/login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: formData.get('token') }),
  })
  loginForm.reset()
  if (!response.ok) {
    showLogin(response.status === 401 ? 'token 不正确，请重新输入。' : '登录失败，请查看后端日志。')
    return
  }
  showPanel()
})

void checkSession().catch(() => showLogin('连接不到后端，请确认服务已启动。'))
