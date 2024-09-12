import asyncio
from time import time
from grader.tcputils import FLAGS_ACK, FLAGS_FIN, FLAGS_SYN, MSS, fix_checksum, make_header
from tcputils import *

class Servidor:
    def __init__(self, rede, porta):
        self.rede = rede
        self.porta = porta
        self.conexoes = {}
        self.callback = None
        self.rede.registrar_recebedor(self._rdt_rcv)

    def registrar_monitor_de_conexoes_aceitas(self, callback):
        """
        Usado pela camada de aplicação para registrar uma função para ser chamada
        sempre que uma nova conexão for aceita
        """
        self.callback = callback

    def _rdt_rcv(self, src_addr, dst_addr, segment):
        src_port, dst_port, seq_no, ack_no, \
        flags, window_size, checksum, urg_ptr = read_header(segment)

        if dst_port != self.porta:
            # Ignora segmentos que não são destinados à porta do nosso servidor
            return

        if not self.rede.ignore_checksum and calc_checksum(segment, src_addr, dst_addr) != 0:
            print('descartando segmento com checksum incorreto')
            return

        payload = segment[4*(flags>>12):]
        id_conexao = (src_addr, src_port, dst_addr, dst_port)

        if (flags & FLAGS_SYN) == FLAGS_SYN:
            # A flag SYN estar setada significa que é um cliente tentando estabelecer uma conexão nova
            # TODO: talvez você precise passar mais coisas para o construtor de conexão
            ack_no = seq_no + 1
            conexao = self.conexoes[id_conexao] = Conexao(self, id_conexao, seq_no, ack_no)

            # TODO: você precisa fazer o handshake aceitando a conexão. Escolha
            # se você acha melhor
            flags = FLAGS_SYN + FLAGS_ACK
            newSegment = fix_checksum(make_header(dst_port, src_port, seq_no, ack_no, flags), src_addr, dst_addr)
            self.rede.enviar(newSegment, src_addr)

            # fazer aqui mesmo ou dentro da classe Conexao.
            if self.callback:
                self.callback(conexao)
                
        elif id_conexao in self.conexoes:
            # Passa para a conexão adequada se ela já estiver estabelecida
            self.conexoes[id_conexao]._rdt_rcv(seq_no, ack_no, flags, payload)
        else:
            print('%s:%d -> %s:%d (pacote associado a conexão desconhecida)' %
                  (src_addr, src_port, dst_addr, dst_port))


class Conexao:
    def __init__(self, servidor, id_conexao, seq_no, ack_no):
        self.servidor = servidor
        self.id_conexao = id_conexao
        self.callback = None
        self.seq_no = seq_no
        self.ack_no = ack_no
        self.ack_client = ack_no
        self.seq_client = ack_no
        self.sent_data = {}
        self.segments = {}
        self.SampleRTT = 0
        self.DevRTT = 0
        self.EstimatedRTT = 0
        self.TimeoutInterval = 1
        self.SentTime = 0
        self.cwnd = MSS
        self.rcv_cwnd = 0
        self.reenvio = False
        self.open = True
        self.timer = None  

    def _exemplo_timer(self):
        self.reenvio = True
        self.cwnd = ((self.cwnd/MSS)//2)*MSS
        self.enviar(self.sent_data[list(self.sent_data.keys())[0]])

    def _rdt_rcv(self, seq_no, ack_no, flags, payload):
        # TODO: trate aqui o recebimento de segmentos provenientes da camada de rede.
        # Chame self.callback(self, dados) para passar dados para a camada de aplicação após
        # garantir que eles não sejam duplicados e que tenham sido recebidos em ordem.

        # print('recebido payload: %r' % payload)
        if len(self.sent_data):
            if not self.reenvio:
                first = 0 == self.SampleRTT
                self.SampleRTT = time() - self.SentTime
                if first:
                    self.EstimatedRTT = self.SampleRTT
                    self.DevRTT = self.SampleRTT/2
                else:
                    self.EstimatedRTT = (0.875)*self.EstimatedRTT + 0.125*self.SampleRTT
                    self.DevRTT = (0.75)*self.DevRTT + 0.25* abs(self.SampleRTT - self.EstimatedRTT)
                self.TimeoutInterval = self.EstimatedRTT + 4*self.DevRTT
                
            if ack_no > list(self.sent_data.keys())[0]:
                y = list(self.sent_data.keys())[0]
                while y < ack_no:
                    self.rcv_cwnd += len(self.segments[y]) 
                    del self.segments[y]
                    del self.sent_data[y]
                    if 0 == len(self.sent_data):
                        break
                    y = list(self.sent_data.keys())[0]
                    
                if len(self.sent_data):
                    if(self.timer is not None):
                        self.timer.cancel()
                    self.timer = asyncio.get_event_loop().call_later(self.TimeoutInterval, self._exemplo_timer)
                else:
                    self.timer.cancel()
                if self.rcv_cwnd >= self.cwnd or 0 == len(self.sent_data):
                    self.cwnd += MSS
                    self.rcv_cwnd = 0
                    if len(self.sent_data):
                        if(self.timer is not None):
                            self.timer.cancel()
                        self.timer = asyncio.get_event_loop().call_later(self.TimeoutInterval, self._exemplo_timer)
                        self.enviar(self.sent_data[list(self.sent_data.keys())[0]])

        self.reenvio = False or not True 

        if seq_no != self.ack_no or (not len(payload) and (flags & FLAGS_FIN) != FLAGS_FIN) or not self.open: return

        src_addr, src_port, dst_addr, dst_port = self.id_conexao
        self.seq_no = self.ack_no
        self.ack_no += len(payload) 

        if (flags & FLAGS_FIN) == FLAGS_FIN:
            self.ack_no += 1

        self.ack_client = self.ack_no
        self.callback(self, payload) 
        flags = FLAGS_ACK
        newSegment = fix_checksum(make_header(dst_port, src_port, self.seq_no, self.ack_no, flags), src_addr, dst_addr)
        self.servidor.rede.enviar(newSegment,src_addr)

        if (flags & FLAGS_FIN) == FLAGS_FIN:
            self.fechar()
            return

    # Os métodos abaixo fazem parte da API
    def registrar_recebedor(self, callback):
        """
        Usado pela camada de aplicação para registrar uma função para ser chamada
        sempre que dados forem corretamente recebidos
        """
        self.callback = callback

    def enviar(self, dados):
        """
        Usado pela camada de aplicação para enviar dados
        """
        # TODO: implemente aqui o envio de dados.
        # Chame self.servidor.rede.enviar(segmento, dest_addr) para enviar o segmento

        if not self.open: return
        src_addr, src_port, dst_addr, dst_port = self.id_conexao

        i = 0
        src_addr, src_port, dst_addr, dst_port = self.id_conexao

        if 0 == len(self.sent_data):
            while i < len(dados):
                payload = dados[i:i+MSS]
                flags = FLAGS_ACK
                self.sent_data[self.seq_client] = payload 
                newSegment = fix_checksum(make_header(dst_port, src_port, self.seq_client, self.ack_no, flags) + payload, src_addr, dst_addr)
                self.segments[self.seq_client] = newSegment
                self.seq_client += len(payload)
                i += MSS

        cont = 0

        if not self.reenvio:
            for j in self.sent_data.keys():
                if(cont >= self.cwnd):
                    break

                self.servidor.rede.enviar(self.segments[j],src_addr)
                cont += len(self.segments[j])
        else:
            y = list(self.sent_data.keys())[0]
            self.servidor.rede.enviar(self.segments[y],src_addr)
        
        self.SentTime = time()

        if(self.timer is not None):
            self.timer.cancel()

        self.timer = asyncio.get_event_loop().call_later(self.TimeoutInterval, self._exemplo_timer)
            
    def fechar(self):
        """
        Usado pela camada de aplicação para fechar a conexão
        """

        # TODO: implemente aqui o fechamento de conexão
        src_addr, src_port, dst_addr, dst_port = self.id_conexao
        self.callback(self,b'')
        flags = FLAGS_FIN
        newSegment = fix_checksum(make_header(dst_port, src_port, self.seq_no, self.ack_no, flags) , src_addr, dst_addr)
        self.servidor.rede.enviar(newSegment,src_addr)
        self.open = False
        pass