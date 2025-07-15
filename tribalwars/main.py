import json
import os
import time
import random
import threading
from datetime import datetime
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import ActionChains
from urllib.parse import urlparse, parse_qs
import re

# Tempo padrão (minutos) quando não houver tropas disponíveis
DELAY_MINUTOS_SEM_TROPA = 2

class TribalWarsBot:
    def __init__(self, driver, semaforo: threading.Semaphore):
        self.driver = driver
        self.semaforo = semaforo
        self.world = None
        self.village_id = None
        self.targets = []             # aldeias bárbaras alvo
        self.player_villages = []     # aldeias do jogador
        self.qtd_cavalaria = 0
        self.alvos_perigosos = set()

        # stealth
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {get: () => false});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','pt']});
                const orig = navigator.permissions.query;
                navigator.permissions.__proto__.query = params =>
                  params.name==='notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : orig(params);
            '''
        })

    def human_delay(self, mean=2.0, std=0.75):
        time.sleep(max(0.1, random.gauss(mean, std)))

    def url(self, path: str) -> str:
        return f"https://{self.world}.tribalwars.com.br/game.php?{path}"

    def extrair_world_village(self):
        parsed = urlparse(self.driver.current_url)
        self.world = parsed.hostname.split('.')[0]
        params = parse_qs(parsed.query)
        self.village_id = int(params.get('village', [0])[0])
        print(f"[{self.world}][{self.village_id}][INIT] Mundo e aldeia inicial detectados")
        return self.world, self.village_id

    def obter_todas_as_aldeias(self):
        try:
            print(f"[{self.world}][{self.village_id}][INIT] Carregando overview_villages para extrair aldeias do jogador...")
            url = self.url(f"village={self.village_id}&screen=overview_villages")
            self.driver.get(url)
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.ID, "production_table"))
            )
            elems = self.driver.find_elements(By.CSS_SELECTOR, "span.quickedit-vn")
            ids = [int(el.get_attribute("data-id")) for el in elems if el.get_attribute("data-id").isdigit()]
            self.player_villages = ids
            print(f"[{self.world}][{self.village_id}][INIT] Aldeias do jogador encontradas: {ids}")
            return ids
        except Exception as e:
            print(f"[{self.world}][{self.village_id}][ERRO] obter_todas_as_aldeias: {e}")
            return []

    def obter_aldeias_barbaras_com_media(self):
        try:
            print(f"[{self.world}][{self.village_id}][MAPA] Acessando tela do mapa...")
            self.driver.get(self.url(f"village={self.village_id}&screen=map"))
            self.wait_random(3, 5)
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script(
                    "return typeof TWMap !== 'undefined' && Object.keys(TWMap.villages).length > 0"
                )
            )
            print(f"[{self.world}][{self.village_id}][MAPA] TWMap carregado. Extraindo aldeias bárbaras e média de pontos...")
            resultado = self.driver.execute_script("""
                try {
                    const barbaras = Object.values(TWMap.villages).filter(v => v.owner == 0);
                    const total = barbaras.length;
                    const soma = barbaras.reduce((acc, v) => acc + Number(v.points || 0), 0);
                    const media = total > 0 ? soma / total : 0;
                    const ids = barbaras.map(v => v.id);
                    return { total, soma, media, ids };
                } catch (e) {
                    return { total: 0, soma: 0, media: 0, ids: [] };
                }
            """)
            print(f"[{self.world}][{self.village_id}][MAPA] Total de bárbaras visíveis: {resultado['total']}")
            print(f"[{self.world}][{self.village_id}][MAPA] Média de pontos: {resultado['media']:.2f}")
            print(f"[{self.world}][{self.village_id}][MAPA] IDs bárbaras extraídos: {resultado['ids']}")
            return resultado['ids'], resultado['media']
        except Exception as e:
            print(f"[{self.world}][{self.village_id}][MAPA] Erro ao extrair bárbaras ou calcular média: {e}")
            return [], 0

    def calcular_cavalarias_necessarias(self, media_pontos, capacidade):
        qtd = max(1, round(media_pontos * 6 / capacidade))
        print(f"[{self.world}][{self.village_id}][CALC] Cavalaria necessária: {qtd}")
        return qtd

    def extrair_segundos_restantes(self, texto: str) -> int:
        try:
            parts = list(map(int, texto.split(':')))
            secs = parts[0]*3600 + parts[1]*60 + (parts[2] if len(parts)==3 else 0)
            return secs
        except:
            return 3600

    def reconectar_se_necessario(self):
        cur = self.driver.current_url
        if 'game.php' not in cur or 'session_expired=1' in cur:
            print(f"[{self.world}][{self.village_id}][RELOGIN] Reconectando sessão...")
            self.driver.get(f"https://www.tribalwars.com.br/page/play/{self.world}")
            self.human_delay(2,1)
            while 'game.php' not in self.driver.current_url:
                time.sleep(1)
            print(f"[{self.world}][{self.village_id}][RELOGIN] Reconectado")

    def obter_total_tropas(self, unidades):
        totais = {}
        try:
            self.driver.get(self.url(f"village={self.village_id}&screen=barracks"))
            self.human_delay(1.5,0.5)
            linhas = self.driver.find_elements(By.CSS_SELECTOR, 'table.vis tr')
            for u in unidades:
                totais[u] = 0
                for l in linhas:
                    if f'data-unit="{u}"' in l.get_attribute('innerHTML'):
                        tds = l.find_elements(By.TAG_NAME, 'td')
                        if len(tds)>=3 and tds[2].text.split('/')[-1].isdigit():
                            totais[u] = int(tds[2].text.split('/')[-1])
                        break
            print(f"[{self.world}][{self.village_id}][INFO] Tropas: {totais}")
        except Exception as e:
            print(f"[{self.world}][{self.village_id}][ERRO] Quartel: {e}")
            for u in unidades:
                totais[u] = 0
        return totais

    def distribuir_tropas_por_peso(self, total: int, slots: int) -> list[int]:
        """
        Divide `total` tropas em `slots` sub‑coletas
        usando as frações definidas:

          1 slot  → tudo em pequena
          2 slots → pequena/média = 2/3, 1/3
          3 slots → pequena/média/grande = 6/11, 3/11, 2/11
          4+ slots→ pequena/média/grande/extrema = 15/26, 6/26, 3/26, 2/26
        """
        # Escolhe numeradores conforme slots
        if slots <= 1:
            nums = [1, 0, 0, 0]
        elif slots == 2:
            nums = [2, 1, 0, 0]
        elif slots == 3:
            nums = [6, 3, 2, 0]
        else:
            nums = [15, 6, 3, 2]

        nums = nums[:slots]
        S = sum(nums)
        if S == 0 or total == 0:
            return [0] * slots

        # Calcula divisão base e distribui resto
        base = [total * n // S for n in nums]
        resto = total - sum(base)
        for i in range(resto):
            base[i % slots] += 1

        return base

    def wait_random(self, min_sec=1.0, max_sec=3.5):
        time.sleep(random.uniform(min_sec, max_sec))

    def bot_coleta(self):
        unidades = ['spear', 'sword', 'axe']
        print(f"[{self.world}][{self.village_id}][COLETA] Iniciando rotina de coleta")
        while "game.php" not in self.driver.current_url:
            time.sleep(2)
        print(f"[{self.world}][{self.village_id}][COLETA] Login confirmado, começando coletas")

        while True:
            try:
                self.semaforo.acquire()
                self.reconectar_se_necessario()

                totais = self.obter_total_tropas(unidades)
                total_spears, total_swords, total_axe = totais['spear'], totais['sword'], totais['axe']
                self.driver.get(self.url(f"village={self.village_id}&screen=place&mode=scavenge"))
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "scavenge-option"))
                )
                self.wait_random(1.5, 2.5)
                blocos = self.driver.find_elements(By.CLASS_NAME, "scavenge-option")

                blocos_indexaveis = [b for b in blocos if "locked-view" not in b.get_attribute('innerHTML')]
                blocos_ocupados = [b for b in blocos_indexaveis if b.find_elements(By.CLASS_NAME, "return-countdown")]

                if not blocos_indexaveis:
                    print(f"[{self.world}][{self.village_id}][COLETA] Nenhum slot disponível")
                    self.semaforo.release()
                    time.sleep(300)
                    continue

                q_spears = self.distribuir_tropas_por_peso(total_spears, len(blocos_indexaveis))
                q_swords = self.distribuir_tropas_por_peso(total_swords, len(blocos_indexaveis))
                q_axe = self.distribuir_tropas_por_peso(total_axe, len(blocos_indexaveis))

                for idx, bloco in enumerate(blocos_indexaveis):
                    if bloco in blocos_ocupados:
                        print(f"[{self.world}][{self.village_id}][COLETA] Slot #{idx+1} ocupado")
                        continue
                    try:
                        for unit, qty in [("spear", q_spears[idx]), ("sword", q_swords[idx]), ("axe", q_axe[idx])]:
                            inp = self.driver.find_element(By.NAME, unit)
                            inp.clear()
                            inp.send_keys(str(qty))
                            time.sleep(0.2)
                        botao = bloco.find_element(By.CSS_SELECTOR, ".free_send_button")
                        self.driver.execute_script("arguments[0].scrollIntoView();", botao)
                        botao.click()
                        print(f"[{self.world}][{self.village_id}][COLETA] Iniciada coleta slot #{idx+1}")
                        time.sleep(1)
                    except Exception as e:
                        print(f"[{self.world}][{self.village_id}][COLETA] Erro slot #{idx+1}: {e}")
                        continue

                # calcula menor retorno
                tempos = []
                for b in blocos_indexaveis:
                    spans = b.find_elements(By.CLASS_NAME, "return-countdown")
                    if spans:
                        hms = spans[0].text.strip().split(":")
                        secs = self.extrair_segundos_restantes(spans[0].text)
                        tempos.append(secs)
                wait = min(tempos) + 10 if tempos else DELAY_MINUTOS_SEM_TROPA*60
                print(f"[{self.world}][{self.village_id}][COLETA] Aguardando {wait//60}m")
                self.semaforo.release()
                time.sleep(wait)
            except Exception as e:
                print(f"[{self.world}][{self.village_id}][COLETA] Erro geral: {e}")
                self.semaforo.release()
                time.sleep(60)

    def tem_cavalaria(self, qtd):
        try:
            inp = self.driver.find_element(By.NAME,'light')
            return int(inp.get_attribute('data-all-count') or 0) >= qtd
        except:
            return False

    def obter_tempo_retorno(self):
        try:
            self.reconectar_se_necessario()
            self.driver.get(self.url(f"village={self.village_id}&screen=place"))
            time.sleep(1)
            rows = self.driver.find_elements(By.CSS_SELECTOR,'#commands_outgoings tr.command-row')
            tempos = [self.extrair_segundos_restantes(r.find_element(By.CSS_SELECTOR,'td:last-child span').text) for r in rows]
            ret = min(tempos)+random.randint(10,30) if tempos else DELAY_MINUTOS_SEM_TROPA*60
            print(f"[{self.world}][{self.village_id}][RETORNO] Tempo retorno {ret}s")
            return ret
        except Exception as e:
            print(f"[{self.world}][{self.village_id}][RETORNO] Erro: {e}")
            return DELAY_MINUTOS_SEM_TROPA*60

    def enviar_cavalaria(self, target_id):
        try:
            self.reconectar_se_necessario()
            self.driver.get(self.url(f"village={self.village_id}&screen=place&target={target_id}"))
            self.human_delay(2,1)
            if not self.tem_cavalaria(self.qtd_cavalaria):
                print(f"[{self.world}][{self.village_id}][ATAQUE] Sem cavalaria suficiente")
                return 'sem_tropas'
            inp = self.driver.find_element(By.NAME,'light')
            inp.clear()
            inp.send_keys(str(self.qtd_cavalaria))
            self.human_delay(0.5,0.2)
            self.driver.find_element(By.ID,'target_attack').click()
            WebDriverWait(self.driver,5).until(
                lambda d: 'try=confirm' in d.current_url or 'screen=info_village' in d.current_url
            )
            if 'try=confirm' in self.driver.current_url:
                btn = WebDriverWait(self.driver,5).until(
                    EC.element_to_be_clickable((By.ID,'troop_confirm_submit'))
                )
                btn.click()
                self.human_delay(1,0.5)
            print(f"[{self.world}][{self.village_id}][ATAQUE] {self.qtd_cavalaria} light -> {target_id}")
            return 'sucesso'
        except Exception as e:
            print(f"[{self.world}][{self.village_id}][ATAQUE] Erro: {e}")
            return 'erro'

    def bot_ataques(self):
        print(f"[{self.world}][{self.village_id}][ATAQUE] Iniciando rotina de ataques")
        caminho_arquivo = f"perdas_{self.world}.json"
        perdas_ids = set(json.load(open(caminho_arquivo)).keys()) if os.path.exists(caminho_arquivo) else set()
        ciclos = 0
        print(f"Ids perigosos {perdas_ids}")
        while True:
            self.semaforo.acquire()
            for tgt in random.sample(self.targets, len(self.targets)):
                if tgt in perdas_ids:
                    print(f"[{self.world}][{self.village_id}][ATAQUE] Pulando alvo perigoso {tgt}")
                    continue
                res = self.enviar_cavalaria(tgt)
                if res == 'sem_tropas':
                    self.semaforo.release()
                    time.sleep(self.obter_tempo_retorno() * random.uniform(1.1, 1.4))
                elif res == 'sucesso':
                    pass
                else:
                    time.sleep(15)
            ciclos += 1
            time.sleep(random.uniform(600, 900))
            if ciclos % 10 == 0:
                self.semaforo.release()
                time.sleep(random.randint(600, 1200))

    def obter_ids_aldeias_com_perdas(self):
        # bloqueia o semáforo para evitar outra navegação durante o processo
        self.semaforo.acquire()
        try:
            self.reconectar_se_necessario()
            print(f"[{self.world}][{self.village_id}][RELATÓRIOS] Buscando relatórios com perdas...")
            caminho_arquivo = f"perdas_{self.world}.json"
            perdas_salvas = json.load(open(caminho_arquivo)) if os.path.exists(caminho_arquivo) else {}

            # coleta links de relatórios com perdas
            links = []
            pagina = 0
            vistos = set()
            while True:
                suffix = f"&from={pagina * 12}" if pagina else ""
                self.driver.get(self.url(f"village={self.village_id}&screen=report&mode=attack{suffix}"))
                self.human_delay(2, 1)

                rows = self.driver.find_elements(By.CSS_SELECTOR, "#report_list tr")[2:]
                novos = 0
                for row in rows:
                    cols = row.find_elements(By.TAG_NAME, "td")
                    if len(cols) < 2:
                        continue
                    td = cols[1]
                    if any(img.get_attribute("data-title") == "Perdas"
                           for img in td.find_elements(By.TAG_NAME, "img")):
                        href = td.find_element(By.CSS_SELECTOR, "a.report-link").get_attribute("href")
                        if href not in vistos:
                            vistos.add(href)
                            links.append(href)
                            novos += 1
                if novos == 0:
                    break
                pagina += 1

            # abre cada relatório e pega o segundo span[data-id]
            for link in links:
                self.driver.get(link)
                self.human_delay(1.5, 0.5)

                spans = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "span.village_anchor.contexted[data-id]"
                )
                # se tiver pelo menos 2, pega o segundo; senão, pega o primeiro
                if len(spans) >= 2:
                    span = spans[1]
                elif spans:
                    span = spans[0]
                else:
                    continue

                vid = span.get_attribute("data-id")
                # ignora se for a própria vila
                if vid == str(self.village_id):
                    continue
                if vid not in perdas_salvas:
                    perdas_salvas[vid] = datetime.now().isoformat()

            # salva e atualiza
            with open(caminho_arquivo, "w") as f:
                json.dump(perdas_salvas, f, indent=2)
            self.alvos_perigosos = set(perdas_salvas.keys())
            print(f"[{self.world}][{self.village_id}][RELATÓRIOS] "
                  f"{len(self.alvos_perigosos)} alvos com perdas registrados")

        except Exception as e:
            print(f"[{self.world}][{self.village_id}][RELATÓRIOS] Erro geral: {e}")
        finally:
            self.semaforo.release()

    def start(self):
        self.extrair_world_village()
        vills = self.obter_todas_as_aldeias()
        self.targets, media = self.obter_aldeias_barbaras_com_media()
        self.qtd_cavalaria = self.calcular_cavalarias_necessarias(media, 80)
        self.obter_ids_aldeias_com_perdas()
        for vid in vills:
            bot = TribalWarsBot(self.driver, self.semaforo)
            bot.world = self.world
            bot.village_id = vid
            bot.targets = self.targets
            bot.qtd_cavalaria = self.qtd_cavalaria
            bot.alvos_perigosos = self.alvos_perigosos
            threading.Thread(target=bot.bot_coleta, daemon=True).start()
            threading.Thread(target=bot.bot_ataques, daemon=True).start()
            print(f"[{self.world}][{vid}][START] Threads iniciadas para coleta e ataque")

if __name__ == '__main__':
    #worlds = ['br135', 'br136']
    worlds = ['br136']
    # 1 único semáforo para sincronizar ambos os mundos
    global_semaforo = threading.Semaphore(1)

    # driver 1 (perfil com sessão salva)
    opts1 = uc.ChromeOptions()
    opts1.add_argument('--no-sandbox')
    opts1.add_argument('--disable-blink-features=AutomationControlled')
    opts1.add_argument('--user-data-dir=C:/bots_tw/sessao')
    d1 = uc.Chrome(options=opts1)
    d1.set_window_size(1280, 800)
    d1.get('https://www.tribalwars.com.br')
    while 'game.php' not in d1.current_url:
        time.sleep(2)
    raw_cookies = d1.get_cookies()

    # driver 2 (clone de sessão)
    """opts2 = uc.ChromeOptions()
    opts2.add_argument('--no-sandbox')
    opts2.add_argument('--disable-blink-features=AutomationControlled')
    d2 = uc.Chrome(options=opts2)
    d2.set_window_size(1280, 800)
    d2.get('https://www.tribalwars.com.br')
    # injeta os cookies do primeiro perfil no segundo
    for c in raw_cookies:
        ck = {'name': c['name'], 'value': c['value'], 'path': c.get('path', '/')}
        if 'expiry' in c: ck['expiry'] = c['expiry']
        if 'secure' in c: ck['secure'] = c['secure']
        d2.add_cookie(ck)
    # já entra diretamente no mundo 2
    d2.get(f"https://www.tribalwars.com.br/page/play/{worlds[1]}")
    while 'game.php' not in d2.current_url:
        time.sleep(1)"""

    # instancia um bot para cada driver/mundo
    bot1 = TribalWarsBot(d1, global_semaforo)
    #bot2 = TribalWarsBot(d2, global_semaforo)
    bot1.start()
    #bot2.start()

    # mantém o script rodando
    while True:
        time.sleep(60)

