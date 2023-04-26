import os 

import numpy as np 
import torch 

from torch.nn import Module, Parameter, Embedding, Linear, Dropout
from torch.nn.init import kaiming_normal_
from torch.nn.functional import binary_cross_entropy
from sklearn import metrics 


class DKVMN(Module):
    '''
        Args:
            num_q: the total number of the questions(KCs) in the given dataset
            dim_s: the dimension of the state vectors in this model
            size_m: the memory size of this model
    '''
    def __init__(self, num_q, dim_s, size_m) -> None:
        super(DKVMN, self).__init__()
        self.num_q = num_q 
        self.dim_s = dim_s 
        self.size_m = size_m

        # M은 각 location의 벡터사이즈, N은 memory location의 수 
        # Mt: N*M matrix
        # 각 문제에는 N latent concepts가 존재한다고 가정
        # 각 가정된 컨셉은 key matrix Mk에 저장 (N * key의 dimension. N은 latent concepts)
        # 각 컨셉의 학생들의 마스터리 value matrix Mv에 저장 (N * value의 dimension)


        # Left network
        # q_t를 받는 부분, 논문상에서 A 과정, K_t라고 볼 수 있음
        self.k_emb_layer = Embedding(self.num_q, self.dim_s)
        self.Mk = Parameter(torch.Tensor(self.size_m, self.dim_s))
        self.Mv0 = Parameter(torch.Tensor(self.size_m, self.dim_s))

        kaiming_normal_(self.Mk)
        kaiming_normal_(self.Mv0)

        # Right network
        self.v_emb_layer = Embedding(2 * self.num_q, self.dim_s)
        self.e_layer = Linear(self.dim_s, self.dim_s)
        self.a_layer = Linear(self.dim_s, self.dim_s)

        # final network
        self.f_layer = Linear(2 * self.dim_s, self.dim_s)
        self.p_layer = Linear(self.dim_s, 1)

        self.dropout_layer = Dropout(0.2)

    
    def forward(self, q, r):
        '''
            Args: 
                q: the question(KC) sequence with the size of [batch_size, n]
                r: the response sequence with the size of [batch_size, n]

            Returns:
                p: the knowledge level about q
                Mv: the value matrices from q, r
        '''

        x = q + self.num_q * r 

        batch_size = x.shape[0]
        # unsqueeze는 지정된 위치에 크기가 1인 텐서 생성 
        # repeat은 현재 갖고 있는 사이즈에 매개변수 만큼 곱해주는 것 (공간 생성, element가 있다면 해당 element 곱해줌.)
        Mvt = self.Mv0.unsqueeze(0).repeat(batch_size, 1, 1) # parameter로 메모리 사이즈만큼, 그리고 임베딩 공간만큼 구해줬음
        Mv = [Mvt]

        # 논문에서 봤던 대로 좌 우측 임베딩.
        k = self.k_emb_layer(q) # 보통의 키는 컨셉 수 
        v = self.v_emb_layer(x) # 컨셉수, 응답 수.. 보통은 
        
        # Correlation Weight
        w = torch.softmax(torch.matmul(k, self.Mk.T), dim=-1) # 차원이 세로로 감, 0, 1, 2 뎁스가 깊어질 수록 가로(행)에 가까워짐, 모든 row 데이터에 대해 softmax 

        # Write Process
        e = torch.sigmoid(self.e_layer(v))
        a = torch.tanh(self.a_layer(v))

        for et, at, wt in zip(
            e.permute(1, 0, 2), a.permute(1, 0, 2), w.permute(1, 0, 2)
        ):
            Mvt = Mvt * (1 - (wt.unsqueeze(-1) * et.unsqueeze(1))) + \
            (wt.unsqueeze(-1) * at.unsqueeze(1))
            Mv.append(Mvt)
        
        Mv = torch.stack(Mv, dim=1)

        # Read Process 
        f = torch.tanh(
            self.f_layer(
            torch.cat(
                [
                    (w.unsqueeze(-1) * Mv[:, :-1]).sum(-2),
                    k
                ],
                dim=-1
            )
            )
        )

        p = self.p_layer(self.dropout_layer(f))

        p = torch.sigmoid(p)
        p = p.squeeze(-1)

        return p, Mv
    
    def train_model(self, train_loader, test_loader, num_epochs, opt, ckpt_path):
        '''
            Args:
                train_loader: the PyTorch DataLoader instance for training
                test_loader: the PyTorch DataLoader instance for test
                num_epochs: the number of epochs
                opt: the optimization to train this model
                ckpt_path: the path to save this model's parameters
        '''
        aucs = []
        loss_means = []  

        max_auc = 0

        for i in range(0, num_epochs):
            loss_mean = []

            for data in train_loader:
                # q_seqs, r_seqs, qshft_seqs, rshft_seqs, mask_seqs, bert_sentences, bert_sentence_types, bert_sentence_att_mask, proc_atshft_sentences
                q, r, _, _, m, bert_s, _, _, _ = data

                self.train()
                
                # 현재까지의 입력을 받은 뒤 다음 문제 예측
                y, _ = self(q.long(), r.long())

                # y와 t 변수에 있는 행렬들에서 마스킹이 true로 된 값들만 불러옴
                y = torch.masked_select(y, m)
                t = torch.masked_select(r, m)

                opt.zero_grad()
                loss = binary_cross_entropy(y, t) # 실제 y^T와 원핫 결합, 다음 answer 간 cross entropy
                loss.backward()
                opt.step()

                loss_mean.append(loss.detach().cpu().numpy())

            with torch.no_grad():
                for data in test_loader:
                    q, r, _, _, m, bert_s, _, _, _ = data

                    self.eval()

                    y, _ = self(q.long(), r.long())

                    # y와 t 변수에 있는 행렬들에서 마스킹이 true로 된 값들만 불러옴
                    y = torch.masked_select(y, m).detach().cpu()
                    t = torch.masked_select(r, m).detach().cpu()

                    auc = metrics.roc_auc_score(
                        y_true=t.numpy(), y_score=y.numpy()
                    )

                    loss_mean = np.mean(loss_mean) # 실제 로스 평균값을 구함
                    
                    print(f"Epoch: {i}, AUC: {auc}, Loss Mean: {loss_mean} ")

                    if auc > max_auc : 
                        torch.save(
                            self.state_dict(),
                            os.path.join(
                                ckpt_path, "model.ckpt"
                            )
                        )
                        max_auc = auc

                    aucs.append(auc)
                    loss_means.append(loss_mean)

        return aucs, loss_means